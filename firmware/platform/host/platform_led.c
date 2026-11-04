/*
 * platform/host/platform_led.c — host 接缝的状态灯空桩
 *
 * host（PC 编译验证）没有硬件 LED，全部空操作。真实实现见
 * platform/esp32/platform_led.c（WS2812@GPIO48 + IO2 用户灯）。
 * API 语义与六态协议见 platform/platform.h 与 esp32 实现头部注释。
 */
#ifdef PLATFORM_HOST

#include "platform/platform.h"

void platform_led_init(void)            { }
void platform_led_set_streaming(bool on) { (void)on; }
void platform_led_set_error(bool on)     { (void)on; }

/* IO2 调试接缝：host 无硬件，pad 读数回 -1（未测），LEDC 视为不可用。 */
void platform_led_set_io2(int duty)      { (void)duty; }
void platform_led_set_io2_pol(int pol)   { (void)pol; }
void platform_led_set_io2_mode(int gpio_mode) { (void)gpio_mode; }
int  platform_led_io2_mode_get(void) { return 0; }
void platform_led_io2_diag(int *ready, int *pad_high, int *pad_low,
                           int *pad_now, int *pad_pct)
{
    if (ready)    *ready    = 0;
    if (pad_high) *pad_high = -1;
    if (pad_low)  *pad_low  = -1;
    if (pad_now)  *pad_now  = -1;
    if (pad_pct)  *pad_pct  = -1;
}

#endif /* PLATFORM_HOST */
