/*
 * main/main.c — 固件入口（零 ESP-IDF 依赖的纯编排层）
 *
 * 设计原则（v1.9.x 接缝决策）：
 *   本文件**不 include 任何 ESP-IDF / NimBLE / esp32-camera 头文件**。
 *   一切硬件与系统调用都经 platform.h 的接缝转达：
 *     platform_system_init / platform_ble_init / platform_wifi_init
 *     / platform_http_start / platform_stream_start
 *   这样：
 *     - host 接缝：本文件可被普通 C 编译器编译（零硬件构建验证）；
 *     - esp32 接缝：到货后换 platform/esp32 下的实现即可，本文件一字不改。
 *
 * 入口约定：两个分支都提供 app_main()（ESP-IDF 的 freertos/app_startup.c
 * 强制引用该符号）。仅在定义 FW_HOST_STANDALONE_MAIN 时额外提供 main()。
 *
 * 与旧版相比的变化：原先内联的 nvs_flash_init() / esp_http_server 回调 /
 * BLEDevice::init() / xTaskCreate() 全部下移到 platform 接缝，
 * 使本文件在任何平台都能编译，也避免依赖 Arduino 生态的 BLEDevice.h。
 */
#include "platform/platform.h"
#include "platform/platform_mdns.h"
#include "main/routes.h"
#include "hid_router/hid_router.h"

/* MJPEG 推流主循环的内容构造：取帧 + 组 multipart 帧。
 * 真实 socket 写入由 platform_stream_start 的实现负责；这里只给出
 * 与纯逻辑模块（camera_next_jpeg / mjpeg_part）的接驳示例。 */
#include "camera/camera.h"
#include "camera/mjpeg.h"

/* MJPEG 边界串来自 routes.h 的单点定义，与 platform_net.c 的 /stream 响应头共用。 */
#define STREAM_BOUNDARY QSHOU_STREAM_BOUNDARY

/* ── BLE 设备名（安卓蓝牙列表 / 配对列表里显示的名字）──────────────
 * 复合 HID 设备（鼠标+键盘+Consumer），用中性产品名 "Qshou"。
 * 设备类型由广播 appearance(GENERIC) + 描述符决定，与名字无关。
 * 要换名字**只改这一行**即可。 */
#define BLE_DEVICE_NAME "Qshou"

/* 由 platform 的推流任务回调：构造一帧 multipart 内容到 out。
 * 返回写入字节数；0 表示本帧跳过。 */
size_t firmware_build_frame(uint8_t *out, size_t cap)
{
    const uint8_t *jpeg = 0;
    size_t jlen = 0;
    if (camera_next_jpeg(&jpeg, &jlen) != 0)
        return 0;
    size_t n = mjpeg_part(out, cap, STREAM_BOUNDARY, jpeg, jlen);
    if (n > 0)
        routes_mark_frame();   /* 供 /api/v1/health 上报 last_frame_ms */
    return n;
}

/* 适配：platform_ble_is_connected 返回 bool，router 的 ready 约定返回 int。
 * 用适配器而不是改 router 签名 —— 让纯逻辑层不依赖 stdbool / 平台头；
 * 将来 USB 后端的 tud_mounted() 同样是 bool，也走各自适配器。 */
static int ble_backend_ready(void)
{
    return platform_ble_is_connected() ? 1 : 0;
}

int app_main_entry(void)
{
    platform_log("MAIN", "Qshou firmware booting...");

    /* 状态灯最先起来：其后每个环节（WiFi/BLE/相机）的状态都能直接从灯上读出，
     * 无需开串口（BLE 测试期间串口是禁区 —— DTR 下降沿会复位板子踢掉连接）。 */
    platform_led_init();

    if (platform_system_init() != 0) {
        platform_log("MAIN", "system init failed");
        return 1;
    }
    /* mDNS 广播：让控制端用 qshou-0a0c.local 访问，IP 漂移也不必扫网段。
     * 失败不致命 —— 仍可走 IP 直连。组件随 STA 拿到 IP 自动开始广播。 */
    if (platform_mdns_start() != 0)
        platform_log("MAIN", "mdns start failed (continuing)");

    /* HID 路由装配：注册 BLE 后端（USB 后端在推流启动后追加，见下方）。
     * 必须在 ble_init 之前 —— 注册只是把回调存起来，ready 是运行时实时查询，
     * 不依赖 BLE 是否已启动；提前注册可保证 BLE 起来后的第一条报文就有路由。 */
    hid_router_register(HID_BACKEND_BLE, ble_backend_ready, platform_ble_emit_report);

    /* 把路由注入平台层：手势回放队列与按键队列跑在平台层常驻任务里，
     * 必须与 HTTP 端点走同一条路由 —— 否则 USB 后端接入后手势/按键仍只走 BLE。 */
    platform_hid_set_emit(hid_router_emit);

    if (platform_ble_init(BLE_DEVICE_NAME) != 0) {
        platform_log("MAIN", "ble init failed");
        return 1;
    }
    /* WiFi 失败不致命：先满足“跑通”，网络环节可后调（见 execution_policy） */
    if (platform_wifi_init() != 0)
        platform_log("MAIN", "wifi init failed (continuing)");

    if (platform_http_start() != 0) {
        platform_log("MAIN", "http start failed");
        return 1;
    }
    if (platform_stream_start() != 0)
        platform_log("MAIN", "stream start failed (continuing)");

    /* USB HID 后端（Phase 1）：原生 USB-OTG 上的 TinyUSB 复合设备
     * （接口0 = Boot Keyboard / 接口1 = 鼠标 + Consumer）。
     * 安装成功才注册进路由 —— 安装失败时 USB 后端根本不存在，AUTO 策略
     * 会直接走 BLE，而不是「选中一个永远不可用的后端」。 */
    if (platform_usb_init() == 0) {
        hid_router_register(HID_BACKEND_USB, platform_usb_ready, platform_usb_emit);
    } else {
        platform_log("MAIN", "usb hid init failed (BLE only)");
    }

    platform_log("MAIN", "Qshou firmware ready");
    return 0;
}

/* ESP-IDF 强制要求名为 app_main 的 C 入口（freertos/app_startup.c 引用它）。
 * 因此**两个接缝分支都必须提供 app_main** —— 否则 host 接缝在 IDF 链接
 * 阶段会报 `undefined reference to 'app_main'`（零硬件构建验证就走不完）。 */
void app_main(void)
{
    (void)app_main_entry();
}

#if defined(FW_HOST_STANDALONE_MAIN)
/* 仅当以“独立 host 可执行文件”方式链接时（如自建 main() 的测试），
 * 才额外提供 C 标准入口。IDF 构建不定义此宏，避免与 app_startup 冲突。 */
int main(void)
{
    return app_main_entry();
}
#endif
