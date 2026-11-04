/*
 * platform_gpio.c — host 桩 GPIO 接缝（P1 内存模拟）
 *
 * host 不连硬件：用一份内存状态表模拟 GPIO 行为，逻辑与 esp32 接缝保持一致
 * （同样的白名单/输入专用脚规则），仅用于零硬件构建与单元测试。
 * 真实 GPIO 驱动只在 esp32 接缝落地（见 platform/esp32/platform_gpio.c）。
 */
#include "platform/platform.h"
#include <string.h>
#include <stdbool.h>
#include <stdint.h>

/* 与 esp32 接缝一致：用户白名单剔除「相机占用」与「八线 PSRAM/MSPI 占用」后的集合。
 * 🔴 26/27/33/34/35 已被剔除 —— 本板 PSRAM 是 Octal 八线，这几脚是 SPI0/MSPI
 *    的片选与数据线（权威来源：esp_hal_gpspi/esp32s3/include/soc/spi_pins.h），碰了会
 *    破坏 PSRAM 访问。 */
static const int GPIO_SAFE[] = {19, 21, 22, 23, 25};
#define GPIO_SAFE_N  (int)(sizeof(GPIO_SAFE) / sizeof(GPIO_SAFE[0]))

/* 与 esp32 接缝同规则的硬守卫（host 侧同样生效，保证契约一致）。 */
static bool host_gpio_is_mspi(int pin)     { return pin >= 26 && pin <= 37; }
static bool host_gpio_is_camera(int pin)   { return pin == 4 || pin == 5 || pin == 16 || pin == 17 || pin == 18; }
static bool host_gpio_allowed(int pin)     { return !host_gpio_is_mspi(pin) && !host_gpio_is_camera(pin); }

typedef struct {
    int          pin;
    gpio_mode_e  mode;
    int          value;
    bool         configured;
} gpio_state_t;

static gpio_state_t s_state[GPIO_SAFE_N];
static bool         s_inited = false;

static bool host_gpio_is_input_only(int pin) { return pin == 34 || pin == 35; }

static void ensure_init(void)
{
    if (s_inited) return;
    for (int i = 0; i < GPIO_SAFE_N; i++) {
        s_state[i].pin        = GPIO_SAFE[i];
        s_state[i].mode       = GPIO_MODE_IN;
        s_state[i].value      = 0;
        s_state[i].configured = false;
    }
    s_inited = true;
}

static int state_idx(int pin)
{
    for (int i = 0; i < GPIO_SAFE_N; i++)
        if (s_state[i].pin == pin) return i;
    return -1;
}

int platform_gpio_list(gpio_pin_info_t *out, int max)
{
    ensure_init();
    if (!out || max <= 0) return 0;
    int n = 0;
    for (int i = 0; i < GPIO_SAFE_N && n < max; i++, n++) {
        out[n].pin   = s_state[i].pin;
        out[n].mode  = s_state[i].mode;
        out[n].value = s_state[i].value;
    }
    return n;
}

int platform_gpio_get(int pin, gpio_pin_info_t *info)
{
    ensure_init();
    if (!info) return -1;
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    info->pin   = pin;
    info->mode  = s_state[idx].mode;
    info->value = s_state[idx].value;
    return 0;
}

int platform_gpio_pin_mode(int pin, gpio_mode_e mode)
{
    ensure_init();
    if (!host_gpio_allowed(pin)) return -1;       /* 🔴 MSPI / 相机脚硬拒绝 */
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (mode == GPIO_MODE_OUT || mode == GPIO_MODE_PWM) {
        if (host_gpio_is_input_only(pin)) return -1;
    }
    if (mode != GPIO_MODE_IN && mode != GPIO_MODE_OUT && mode != GPIO_MODE_PWM)
        return -1;
    s_state[idx].mode       = mode;
    s_state[idx].configured = true;
    if (mode == GPIO_MODE_IN) s_state[idx].value = 0;
    return 0;
}

int platform_gpio_write(int pin, int value)
{
    ensure_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_OUT) return -1;
    s_state[idx].value = value ? 1 : 0;
    return 0;
}

int platform_gpio_read(int pin)
{
    ensure_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_IN) return -1;
    return s_state[idx].value;
}

int platform_gpio_pwm(int pin, int duty)
{
    ensure_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_PWM) return -1;
    if (duty < 0)   duty = 0;
    if (duty > 255) duty = 255;
    s_state[idx].value = duty;
    return 0;
}

int platform_gpio_pwm_set_freq(int hz)
{
    (void)hz;            /* host 无时钟，频率仅占位 */
    return 0;
}
