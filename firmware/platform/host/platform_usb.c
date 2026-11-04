/*
 * platform/host/platform_usb.c — USB HID 后端的 host 桩
 *
 * 零硬件构建验证用：无 USB 硬件，所有接缝都返回「不可用」。
 * 与 platform/esp32/platform_usb.c 提供同名符号，确保 host 分支也能链接通过
 * （见 main/CMakeLists.txt 的 PLATFORM_KIND 二选一）。
 *
 * 注意 ready 恒返回 0 而不是「无条件可用」：host 构建没有真实 USB，
 * 若谎报可用，hid_router 的 AUTO 策略会选中 USB 并把帧全丢在这里。
 */
#ifdef PLATFORM_HOST

#include "platform/platform.h"

int platform_usb_init(void)
{
    return 0;   /* 无硬件：安装本身不算失败，只是永远不可用 */
}

int platform_usb_ready(void)
{
    return 0;   /* 永远未挂载 → router 回落 BLE */
}

int platform_usb_emit(uint8_t report_id, const uint8_t *data, size_t len)
{
    (void)report_id; (void)data; (void)len;
    return -1;
}

#endif /* PLATFORM_HOST */
