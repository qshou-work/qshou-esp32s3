/*
 * platform/esp32/platform_usb.c — ESP32-S3 原生 USB-OTG HID 后端（Phase 1）
 *
 * 目的（见 docs/usb_hid_plan.md §12）：把 `hid_router` 的 USB 后端接上 TinyUSB，
 * 让 BLE 侧的鼠标 / 键盘 / Consumer 报文能从**原生 USB 口**送达被控机。
 *
 * 为什么值得做（与 BLE HID 的本质区别）：
 *   BLE HID 是**带内**控制 —— 被控机必须已启动 OS 且蓝牙栈可用；
 *   USB HID 是**带外**控制 —— 被控机只把它当一根普通键盘，
 *   因此 **BIOS/UEFI、远程装机、OS 崩溃救援** 这些 KVM 核心场景才成立。
 *
 * ⚠️ 与控制台互斥（本方案的前提，必须遵守）：
 *   ESP32-S3 上 USB-OTG 与 USB-Serial-JTAG **共享同一组 USB PHY（GPIO19/20）**，
 *   二者不能同时使用。因此本模块启用时必须关掉
 *   CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG（见 sdkconfig）。
 *   主控制台是 UART0（GPIO43-44 / CH343），日志与烧写**不受影响**；
 *   且 ROM 的 USB 下载走 USB-Serial-JTAG 而非 OTG，故**不会变砖** ——
 *   救砖永远走 CH343 UART 口。
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 接口划分（BIOS 兼容的关键，改动前先读 usb_hid/usb_hid_frame.h 头注释）
 * ═══════════════════════════════════════════════════════════════════════
 *   接口 0  Boot Keyboard   subclass=Boot protocol=Keyboard  8B 无 Report ID
 *   接口 1  Pointer+Consumer  RID 1 = 鼠标 4B / RID 2 = Consumer 3B
 *
 * ⚠️ 键盘**必须**单独占接口 0 且排第一：
 *   - boot 协议规定键盘报文是固定 8 字节、无 Report ID 前缀；把键盘与鼠标
 *     混在同一接口并加前缀，它就不再是 boot 设备 → **进 BIOS 按键无效**。
 *   - 部分 BIOS 只探测第一个 HID 接口。
 *   这两个错误在 Windows/Linux 上**测不出来**（都接受带 RID 的键盘），
 *   只在 BIOS/UEFI 里暴露 —— 所以由契约测试锁死（test_firmware_contract.py §19e）。
 *
 * 依赖：espressif/esp_tinyusb（见 main/idf_component.yml）
 *       且 **CONFIG_TINYUSB_HID_COUNT 必须 = 2**（下方 #error 会拦住）。
 */
#ifdef PLATFORM_ESP32

#include <stdio.h>
#include <string.h>
#include <stdbool.h>

#include "platform/platform.h"
#include "usb_hid/usb_hid_frame.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "class/hid/hid_device.h"

#define TAG "QSHOU_USB"

/* ⚠️ 编译期闸门：接口 0 与接口 1 是两个 HID 实例，HID_COUNT=1 时
 *    TUD_HID_DESCRIPTOR 的第二个实例会指向不存在的实例（配置描述符长度也算错），
 *    运行时表现为「设备能枚举但接口 1 无响应」——极难定位。
 *    Phase 0 就踩过一次同类坑（HID_COUNT 默认 0 → 固件根本没有 HID 功能）。 */
#if !defined(CFG_TUD_HID) || (CFG_TUD_HID != 2)
#error "本后端要求 CONFIG_TINYUSB_HID_COUNT=2（接口0=键盘 / 接口1=鼠标+Consumer）"
#endif

/* ── 报文描述符 ──────────────────────────────────────────────────────
 * 接口 0：Boot Keyboard，**不传 HID_REPORT_ID()** → 生成无 Report ID 的
 *         标准 boot 描述符（8 字节：[modifier][reserved][k0..k5]）。
 *
 * 接口 1：RID 1 鼠标（**3 字节 payload**）+ RID 2 Consumer（2 字节 usage）。
 *         两个 Report ID 都写前缀，与 usb_hid_pack() 的线帧严格对应。
 */
static const uint8_t s_kbd_report_desc[] = {
    TUD_HID_REPORT_DESC_KEYBOARD()
};

/* ⚠️⚠️ 鼠标描述符**必须手写**，不能用 TUD_HID_REPORT_DESC_MOUSE()。
 *
 *   那个宏生成的报告是 **5 字节**：
 *       buttons(1) + X(1) + Y(1) + **垂直滚轮(1)** + **横向滚轮(1)**
 *   而 BLE 侧的鼠标帧是 3 字节 [buttons][dX][dY]（见 hid_report.h），
 *   线上 4 字节（RID + 3）。host 按描述符期望 6 字节、实际收到 4 字节 →
 *   **静默丢弃全部鼠标报告**：光标不动、设备管理器一切正常、没有任何错误。
 *   首次接后端时踩中，排查了很久（键盘正常、只有鼠标和 Consumer 不动）。
 *
 *   这里手写 3 字节版本：3 buttons + 5 bit padding + X + Y。
 *   与 usb_hid_frame.c 的 USB_HID_FRAME_MOUSE=4（含 RID）严格对齐。 */

/* 接口 1 的报文描述符：鼠标(RID 1) + Consumer(RID 2) 共用一个接口。
 * 两个 Report ID 在同一份描述符里顺序声明，host 按 RID 区分报告。 */
static const uint8_t s_ptr_report_desc[] = {
    /* ── Report ID 1：相对鼠标，3 字节 payload ────────────────────── */
    HID_USAGE_PAGE  ( HID_USAGE_PAGE_DESKTOP     ),
    HID_USAGE       ( HID_USAGE_DESKTOP_MOUSE    ),
    HID_COLLECTION  ( HID_COLLECTION_APPLICATION ),
        HID_REPORT_ID ( USB_HID_RID_MOUSE )
        HID_USAGE      ( HID_USAGE_DESKTOP_POINTER ),
        HID_COLLECTION ( HID_COLLECTION_PHYSICAL ),
            /* 3 个按键（左/右/中），剩余 5 bit 补齐成 1 字节 */
            HID_USAGE_PAGE  ( HID_USAGE_PAGE_BUTTON ),
            HID_USAGE_MIN   ( 1 ),
            HID_USAGE_MAX   ( 3 ),
            HID_LOGICAL_MIN ( 0 ),
            HID_LOGICAL_MAX ( 1 ),
            HID_REPORT_COUNT( 3 ),
            HID_REPORT_SIZE ( 1 ),
            HID_INPUT       ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),
            HID_REPORT_COUNT( 1 ),
            HID_REPORT_SIZE ( 5 ),
            HID_INPUT       ( HID_CONSTANT ),
            /* 相对位移 [-127, 127] */
            HID_USAGE_PAGE  ( HID_USAGE_PAGE_DESKTOP ),
            HID_USAGE       ( HID_USAGE_DESKTOP_X ),
            HID_USAGE       ( HID_USAGE_DESKTOP_Y ),
            HID_LOGICAL_MIN ( 0x81 ),   /* -127 */
            HID_LOGICAL_MAX ( 0x7f ),   /*  127 */
            HID_REPORT_COUNT( 2 ),
            HID_REPORT_SIZE ( 8 ),
            HID_INPUT       ( HID_DATA | HID_VARIABLE | HID_RELATIVE ),
        HID_COLLECTION_END,
    HID_COLLECTION_END,

    /* ── Report ID 2：Consumer Control，2 字节 payload ──────────────
     * 与 BLE 侧 [usage_lo][usage_hi] 一致（16-bit usage）。
     * 这部分等价于 TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(2))，
     * 展开出来是为了让整份描述符在一处可读。 */
    HID_USAGE_PAGE  ( HID_USAGE_PAGE_CONSUMER    ),
    HID_USAGE       ( HID_USAGE_CONSUMER_CONTROL ),
    HID_COLLECTION  ( HID_COLLECTION_APPLICATION ),
        HID_REPORT_ID ( USB_HID_RID_CONSUMER )
        HID_LOGICAL_MIN  ( 0x00            ),
        HID_LOGICAL_MAX_N( 0x03FF, 2       ),
        HID_USAGE_MIN    ( 0x00            ),
        HID_USAGE_MAX_N  ( 0x03FF, 2       ),
        HID_REPORT_COUNT ( 1               ),
        HID_REPORT_SIZE  ( 16              ),
        HID_INPUT        ( HID_DATA | HID_ARRAY | HID_ABSOLUTE ),
    HID_COLLECTION_END
};

/* ── 字符串描述符 ────────────────────────────────────────────────────
 * index 3（序列号）在 init 时用本机 MAC 填充 —— 两板可区分（与 mDNS 同思路）。 */
static char s_serial[16] = "000000000000";
static const char *s_string_desc[6] = {
    (char[]){0x09, 0x04},   /* 0: 语言 = 英语 (0x0409) */
    "Qshou",                 /* 1: 厂商 */
    "Qshou HID",         /* 2: 产品名 */
    NULL,                   /* 3: 序列号（init 时指向 s_serial） */
    "Qshou Boot Keyboard",   /* 4: 接口 0 */
    "Qshou Pointer",         /* 5: 接口 1 */
};

/* 配置描述符总长 = 配置头 + 每个 HID 接口的描述符 */
#define TUSB_DESC_TOTAL_LEN (TUD_CONFIG_DESC_LEN + CFG_TUD_HID * TUD_HID_DESC_LEN)

static const uint8_t s_config_desc[] = {
    /* 配置号, 接口数(2), 字符串索引, 总长, 属性(0 = 总线供电), 最大电流 mA */
    TUD_CONFIG_DESCRIPTOR(1, 2, 0, TUSB_DESC_TOTAL_LEN, 0, 100),

    /* ⚠️ 第 3 参数 = boot_protocol。传 HID_ITF_PROTOCOL_KEYBOARD 才会把
     *    bInterfaceSubClass 置为 Boot(1)、bInterfaceProtocol 置为 Keyboard(1)
     *    —— 这是 BIOS/UEFI 能识别键盘的**必要条件**。
     *    （官方 tusb_hid 示例此处传 false，是普通非 boot 设备，进不了 BIOS。）
     * 接口号, 字符串索引, boot 协议, 报文描述符长度, EP IN, EP 大小, 轮询间隔 ms */
    TUD_HID_DESCRIPTOR(USB_HID_ITF_KEYBOARD, 4, HID_ITF_PROTOCOL_KEYBOARD,
                       sizeof(s_kbd_report_desc), 0x81, 8, 10),

    /* 接口 1：非 boot（HID_ITF_PROTOCOL_NONE），鼠标 + Consumer 靠 Report ID 区分 */
    TUD_HID_DESCRIPTOR(USB_HID_ITF_POINTER, 5, HID_ITF_PROTOCOL_NONE,
                       sizeof(s_ptr_report_desc), 0x82, 8, 10),
};

/* ── 发送互斥锁 ──────────────────────────────────────────────────────
 * emit() 会被多个任务调用（HTTP 处理任务 / gesture_task），而 TinyUSB 的
 * HID FIFO 写入不是线程安全的 —— 并发写会交错出畸形报文。
 * 拿不到锁就丢帧（不阻塞业务任务，也不排队堆积）。 */
static SemaphoreHandle_t s_tx_lock;

/* ── TinyUSB HID 类回调 ──────────────────────────────────────────── */
uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance)
{
    /* instance 就是 HID 实例号（= 接口号）。接口 0 键盘、接口 1 指针。 */
    return (instance == USB_HID_ITF_KEYBOARD) ? s_kbd_report_desc : s_ptr_report_desc;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                               hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen)
{
    (void)instance; (void)report_id; (void)report_type; (void)buffer; (void)reqlen;
    return 0;   /* 返回 0 → 协议栈 STALL 该请求（我们无需支持 GET_REPORT） */
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                           hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize)
{
    (void)instance; (void)report_id; (void)report_type; (void)buffer; (void)bufsize;
}

/* ── 总线事件回调（诊断用：枚举 / 断开在串口日志上可见）────────────
 * ⚠️ 必须走 esp_tinyusb 的 event_cb 注册机制，**不能**自己定义
 *    tud_mount_cb / tud_umount_cb —— 这两个符号
 *    esp_tinyusb/tinyusb.c 已**无条件定义**（内部用于 MSC 挂载与电源管理，
 *    再转调 s_ctx.event_cb）。自己定义会链接失败：
 *      multiple definition of `tud_mount_cb'  ← 首次构建即踩中
 *    tud_suspend_cb / tud_resume_cb 同样由该组件的 Kconfig
 *    （CONFIG_TINYUSB_SUSPEND_CALLBACK / _RESUME_CALLBACK，默认 n）门控，
 *    故这里一律不用裸回调，只注册事件。 */
static void usb_event_cb(tinyusb_event_t *event, void *arg)
{
    (void)arg;
    switch (event->id) {
    case TINYUSB_EVENT_ATTACHED:
        ESP_LOGI(TAG, "USB 已被主机枚举（键盘 + 鼠标/Consumer 就绪，路由将优先走 USB）");
        break;
    case TINYUSB_EVENT_DETACHED:
        ESP_LOGW(TAG, "USB 已断开（HID 路由回落到 BLE）");
        break;
    default:
        break;
    }
}

/* ── 后端接缝：ready / emit ──────────────────────────────────────── */

int platform_usb_ready(void)
{
    return tud_mounted() ? 1 : 0;
}

int platform_usb_emit(uint8_t report_id, const uint8_t *data, size_t len)
{
    /* 未挂载直接失败：router 会据此回落 BLE（铁律 2）。不在这里重试。 */
    if (!tud_mounted() || !s_tx_lock)
        return -1;

    /* BLE 语义 Report ID → USB 线帧（接口/Report ID/字节全在这层定死） */
    uint8_t frame[USB_HID_FRAME_MAX];
    usb_hid_frame_meta_t meta;
    int n = usb_hid_pack(report_id, data, len, frame, sizeof(frame), &meta);
    if (n <= 0)
        return -1;      /* 未知 RID 或长度不符：宁可丢帧，也不发错位报文 */

    /* ⚠️⚠️ 必须剥掉 usb_hid_pack 写的前缀 —— TinyUSB 的 tud_hid_n_report()
     *   在 report_id != 0 时**自己会写** epin[0] = report_id 并把 len++：
     *
     *      if (report_id) { p_epbuf->epin[0] = report_id; memcpy(epin+1, ...); len++; }
     *
     *   若把带前缀的 frame 原样传进去，线上就是 [01][01][btn][dx][dy] ——
     *   双重 Report ID，整帧错位 1 字节。现象：鼠标乱跳、键盘乱按，
     *   而 host 不报任何错（它按描述符硬解析）。第一次接后端时踩中。
     *
     *   usb_hid_pack() 的产出仍然是「USB 线上完整字节」，用于契约测试与文档；
     *   后端只借用它算出的 (接口, RID, 长度)，数据部分去掉前缀后交给协议栈。 */
    const uint8_t *payload = frame;
    uint16_t plen = (uint16_t)n;
    if (meta.rid != USB_HID_RID_NONE) {
        payload = frame + 1;        /* 跳过我们写的前缀，由 TinyUSB 补 */
        plen    = (uint16_t)(n - 1);
    }

    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(10)) != pdTRUE)
        return -1;      /* 上一帧还在发：丢这一帧（鼠标位移丢帧可接受，不排队） */

    /* tud_hid_n_report(instance, report_id, ...) —— 复合设备必须用带 instance
     * 的版本；tud_hid_report() 等价于 instance=0，会把鼠标帧发进键盘接口。
     * 键盘 rid=0 → 协议栈不写前缀，首字节就是 modifier（boot 格式）。 */
    bool ok = tud_hid_n_report(meta.itf, meta.rid, payload, plen);

    xSemaphoreGive(s_tx_lock);
    return ok ? 0 : -1;
}

/* ── 初始化 ───────────────────────────────────────────────────────── */
int platform_usb_init(void)
{
    /* 序列号用本机 WiFi STA MAC 派生（与 mDNS 主机名同思路，两板可区分） */
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_serial, sizeof(s_serial), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    s_string_desc[3] = s_serial;

    s_tx_lock = xSemaphoreCreateMutex();
    if (!s_tx_lock) {
        ESP_LOGE(TAG, "发送互斥锁创建失败");
        return -1;
    }

    tinyusb_config_t cfg = TINYUSB_DEFAULT_CONFIG();
    cfg.descriptor.device            = NULL;    /* 用默认设备描述符 */
    cfg.descriptor.full_speed_config = s_config_desc;
    cfg.descriptor.string            = s_string_desc;
    cfg.descriptor.string_count      = sizeof(s_string_desc) / sizeof(s_string_desc[0]);
    cfg.event_cb                     = usb_event_cb;    /* 枚举 / 断开日志（见上） */
    cfg.event_arg                    = NULL;

    esp_err_t err = tinyusb_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_driver_install 失败: %s", esp_err_to_name(err));
        return -1;
    }

    ESP_LOGI(TAG, "TinyUSB 已安装：接口0=Boot Keyboard（无 RID）/ 接口1=鼠标 RID1 + "
                  "Consumer RID2；序列号 %s", s_serial);
    /* 描述符字节数打进日志：接后端时「描述符声明的报告长度」与「usb_hid_pack
     * 产出的线帧长度」不一致会被 host 静默丢弃（鼠标踩过），有长度可核对。 */
    ESP_LOGI(TAG, "报文描述符：键盘 %u B / 指针 %u B（鼠标 payload 3B + Consumer 2B）",
             (unsigned)sizeof(s_kbd_report_desc), (unsigned)sizeof(s_ptr_report_desc));
    return 0;
}

#endif /* PLATFORM_ESP32 */
