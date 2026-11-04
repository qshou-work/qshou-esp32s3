/*
 * platform_gpio.c — ESP32 GPIO 接缝（P1 真实驱动）
 *
 * 安全模型（见 platform.h 顶注「危险引脚一律拒绝」）：
 *
 *   可管理集 = 用户确认白名单  ∩  未被其它子系统占用  ∩  非输入专用脚
 *
 *   用户确认白名单：IO4/5/16/17/18/19/21/22/23/25/26/27/33/34/35
 *
 *   剔除一：活体 OV2640 相机总线（platform_camera.c 的 QSHOU_CAM_PIN_*）
 *      若在运行时被 GPIO 驱动接管，相机会直接挂掉：
 *          IO4 = SIOD(SCCB SDA)   IO5 = SIOC(SCCB SCL)
 *          IO16 = D7              IO17 = D6              IO18 = D5
 *
 *   剔除二（本板型实锤复核，见下方 MSPI 表）：
 *      本板 flash/PSRAM 为【八线 Octal】（sdkconfig: SPIRAM_MODE_OCT=y,
 *      CS_IO=26, CLK_IO=30），占用 SPI0/MSPI 一整组固定引脚。这些脚在
 *      esp32s3/include/soc/spi_pins.h 硬连线，不可改映射到 GPIO 矩阵：
 *          CS1=26(PSRAM片选) HD=27 WP=28 CS0=29(flash片选) CLK=30
 *          MISO=31 MOSI=32 D4=33 D5=34 D6=35 D7=36 DQS=37
 *      → 26/27/33/34/35 全部是 PSRAM 数据/片选线，绝非空闲 GPIO。
 *      gpio_set_direction 只在 pad 层改 IOMUX，寄存器层面「能用」，但会
 *      把该脚从 PSRAM 总线上摘掉 → 任何后续 PSRAM 访问都可能读到垃圾数据
 *      或随机崩溃。属潜伏数据损坏炸弹，必须拒绝。
 *
 *   ⇒ 实际可管理集 = {19, 21, 22, 23, 25}（仅 5 脚）。
 *      已全树搜索确认这 5 脚未被相机/LED/I2C/按键等任何子系统占用。
 *
 *   另有两道保险：
 *     1) 输入专用脚：ESP32-S3 上 GPIO46/47 等无输出驱动器者拒绝 OUT/PWM
 *        （当前白名单已不含这类脚，保留判定以防未来扩充）。
 *     2) gpio_is_mspi() 运行时硬守卫：即使未来有人扩充 GPIO_SAFE，
 *        命中 MSPI/PSRAM 引脚的请求一律拒绝并打日志。
 *
 * PWM：使用 LEDC_TIMER_3 + 独立 channel 池(2..7)，避开相机(LEDC_TIMER_0/CH0)与
 *   用户灯(LEDC_TIMER_1/CH1) 占用的 timer/channel。ESP32-S3 所有 LEDC timer 必须
 *   共用同一时钟源，故 clk_cfg 取 LEDC_AUTO_CLK 与它们一致。
 *
 * 编译：仅 PLATFORM_ESP32。host 桩见 platform/host/platform_gpio.c。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"

#include <string.h>
#include <stdbool.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"

static const char *TAG = "QSHOU_GPIO";

/* 实际可管理引脚（用户白名单剔除「相机占用」与「八线 PSRAM/MSPI 占用」后的集合）。
 * 🔴 26/27/33/34/35 已被剔除 —— 本板 PSRAM 是 Octal 八线，这几脚是 SPI0/MSPI
 *    的片选与数据线（见文件头），碰了会破坏 PSRAM 访问。 */
static const int GPIO_SAFE[] = {19, 21, 22, 23, 25};
#define GPIO_SAFE_N  (int)(sizeof(GPIO_SAFE) / sizeof(GPIO_SAFE[0]))

/* SPI0/MSPI(flash + Octal PSRAM) 固定引脚黑名单。
 * 权威来源：esp-idf components/esp_hal_gpspi/esp32s3/include/soc/spi_pins.h
 *   CS1=26 HD=27 WP=28 CS0=29 CLK=30 MISO=31 MOSI=32
 *   D4=33 D5=34 D6=35 D7=36 DQS=37
 * 这些脚硬连线到 MSPI，不经 GPIO 矩阵，任何「当普通 GPIO 用」的操作都会
 * 把引脚从 PSRAM/flash 总线上摘掉 → 数据损坏或崩溃。一律拒绝。 */
static bool gpio_is_mspi(int pin)
{
    return (pin >= 26 && pin <= 37);
}

/* 活体子系统占用脚（相机 OV2640）。同样硬拒绝，避免白名单被误扩充。 */
static bool gpio_is_camera(int pin)
{
    return pin == 4 || pin == 5 || pin == 16 || pin == 17 || pin == 18;
}

/* 引脚是否允许被本驱动接管。 */
static bool gpio_pin_allowed(int pin)
{
    if (gpio_is_mspi(pin)) {
        ESP_LOGE(TAG, "pin %d is SPI0/MSPI(flash/PSRAM) line, reject", pin);
        return false;
    }
    if (gpio_is_camera(pin)) {
        ESP_LOGE(TAG, "pin %d is used by OV2640 camera bus, reject", pin);
        return false;
    }
    return true;
}

/* ESP32-S3 输入专用脚（无输出驱动器）。 */
static bool gpio_is_input_only(int pin)
{
    return pin == 46 || pin == 47;
}

/* ---- 每脚运行时状态 ---- */
typedef struct {
    int          pin;
    gpio_mode_e  mode;       /* GPIO_MODE_IN / OUT / PWM；默认 IN */
    int          value;      /* OUT:0/1  PWM:0..255  IN:最近读回 */
    bool         configured; /* 是否已显式 gpio_config */
    int          pwm_ch;     /* PWM channel 索引(0..PWM_CH_N-1)，-1=非 PWM */
} gpio_state_t;

static gpio_state_t s_state[GPIO_SAFE_N];
static bool         s_state_inited = false;

/* ---- PWM(LEDC) 资源 ---- */
#define PWM_TIMER   LEDC_TIMER_3
#define PWM_SPEED   LEDC_LOW_SPEED_MODE
#define PWM_RES     LEDC_TIMER_8_BIT
#define PWM_DEF_HZ  5000
static const ledc_channel_t PWM_CH[] = {
    LEDC_CHANNEL_2, LEDC_CHANNEL_3, LEDC_CHANNEL_4,
    LEDC_CHANNEL_5, LEDC_CHANNEL_6, LEDC_CHANNEL_7,
};
#define PWM_CH_N  (int)(sizeof(PWM_CH) / sizeof(PWM_CH[0]))

static bool s_ch_used[PWM_CH_N];
static bool s_pwm_timer_ok = false;
static int  s_pwm_hz       = PWM_DEF_HZ;

static void ensure_state_init(void)
{
    if (s_state_inited) return;
    for (int i = 0; i < GPIO_SAFE_N; i++) {
        s_state[i].pin        = GPIO_SAFE[i];
        s_state[i].mode       = GPIO_MODE_IN;
        s_state[i].value      = 0;
        s_state[i].configured = false;
        s_state[i].pwm_ch     = -1;
    }
    memset(s_ch_used, 0, sizeof(s_ch_used));
    s_state_inited = true;
}

static int state_idx(int pin)
{
    for (int i = 0; i < GPIO_SAFE_N; i++)
        if (s_state[i].pin == pin) return i;
    return -1;
}

static int pwm_alloc(void)
{
    for (int c = 0; c < PWM_CH_N; c++)
        if (!s_ch_used[c]) { s_ch_used[c] = true; return c; }
    return -1;
}

static void pwm_free_pin(int pin)
{
    for (int i = 0; i < GPIO_SAFE_N; i++) {
        if (s_state[i].pin == pin && s_state[i].pwm_ch >= 0) {
            int c = s_state[i].pwm_ch;
            if (c >= 0 && c < PWM_CH_N) s_ch_used[c] = false;
            s_state[i].pwm_ch = -1;
            return;
        }
    }
}

static void pwm_timer_ensure(void)
{
    if (s_pwm_timer_ok) return;
    ledc_timer_config_t t = {
        .speed_mode       = PWM_SPEED,
        .timer_num        = PWM_TIMER,
        .duty_resolution  = PWM_RES,
        .freq_hz          = s_pwm_hz,
        .clk_cfg          = LEDC_AUTO_CLK,
    };
    if (ledc_timer_config(&t) == ESP_OK) {
        s_pwm_timer_ok = true;
        ESP_LOGI(TAG, "PWM timer ready @%dHz", s_pwm_hz);
    } else {
        ESP_LOGE(TAG, "PWM timer config failed");
    }
}

/* ---- 公开 API ---- */

int platform_gpio_list(gpio_pin_info_t *out, int max)
{
    ensure_state_init();
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
    ensure_state_init();
    if (!info) return -1;
    int idx = state_idx(pin);
    if (idx < 0) return -1;   /* 不在白名单 */
    info->pin   = pin;
    info->mode  = s_state[idx].mode;
    info->value = s_state[idx].value;
    return 0;
}

int platform_gpio_pin_mode(int pin, gpio_mode_e mode)
{
    ensure_state_init();
    if (!gpio_pin_allowed(pin)) return -1;        /* 🔴 MSPI / 相机脚硬拒绝 */
    int idx = state_idx(pin);
    if (idx < 0) return -1;                       /* 不在白名单 */

    if (mode == GPIO_MODE_OUT || mode == GPIO_MODE_PWM) {
        if (gpio_is_input_only(pin)) {
            ESP_LOGW(TAG, "pin %d is input-only, reject OUT/PWM", pin);
            return -1;
        }
    }
    /* 切离 PWM：释放旧 channel */
    if (s_state[idx].pwm_ch >= 0 && mode != GPIO_MODE_PWM)
        pwm_free_pin(pin);

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << pin,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    if (mode == GPIO_MODE_IN) {
        io.mode = GPIO_MODE_INPUT;
    } else if (mode == GPIO_MODE_OUT) {
        io.mode = GPIO_MODE_OUTPUT;
    } else if (mode == GPIO_MODE_PWM) {
        io.mode = GPIO_MODE_OUTPUT;               /* LEDC 接管 pad */
        pwm_timer_ensure();
        if (!s_pwm_timer_ok) return -1;
        int c = s_state[idx].pwm_ch;
        if (c < 0) {
            c = pwm_alloc();
            if (c < 0) {
                ESP_LOGW(TAG, "no free PWM channel for pin %d", pin);
                return -1;
            }
            s_state[idx].pwm_ch = c;
        }
        ledc_channel_config_t ch = {
            .gpio_num   = pin,
            .speed_mode = PWM_SPEED,
            .channel    = PWM_CH[c],
            .timer_sel  = PWM_TIMER,
            .duty       = 0,
            .hpoint     = 0,
        };
        if (ledc_channel_config(&ch) != ESP_OK) {
            pwm_free_pin(pin);
            return -1;
        }
    } else {
        return -1;                               /* 未知模式 */
    }

    if (gpio_config(&io) != ESP_OK) {
        if (mode == GPIO_MODE_PWM) pwm_free_pin(pin);
        return -1;
    }
    s_state[idx].mode       = mode;
    s_state[idx].configured = true;
    return 0;
}

int platform_gpio_write(int pin, int value)
{
    ensure_state_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_OUT) return -1;   /* 须先设为 OUT */
    gpio_set_level((gpio_num_t)pin, value ? 1 : 0);
    s_state[idx].value = value ? 1 : 0;
    return 0;
}

int platform_gpio_read(int pin)
{
    ensure_state_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_IN) return -1;    /* 仅 IN 模式可读 */
    int v = gpio_get_level((gpio_num_t)pin);
    s_state[idx].value = v;
    return v;
}

int platform_gpio_pwm(int pin, int duty)
{
    ensure_state_init();
    int idx = state_idx(pin);
    if (idx < 0) return -1;
    if (s_state[idx].mode != GPIO_MODE_PWM) return -1;
    if (s_state[idx].pwm_ch < 0) return -1;
    if (duty < 0)   duty = 0;
    if (duty > 255) duty = 255;
    ledc_set_duty_and_update(PWM_SPEED, PWM_CH[s_state[idx].pwm_ch],
                             (uint32_t)duty, 0);
    s_state[idx].value = duty;
    return 0;
}

int platform_gpio_pwm_set_freq(int hz)
{
    int f = (hz > 0) ? hz : PWM_DEF_HZ;          /* 0 = 驱动默认 */
    s_pwm_hz = f;
    if (s_pwm_timer_ok) {
        ledc_timer_config_t t = {
            .speed_mode       = PWM_SPEED,
            .timer_num        = PWM_TIMER,
            .duty_resolution  = PWM_RES,
            .freq_hz          = f,
            .clk_cfg          = LEDC_AUTO_CLK,
        };
        ledc_timer_config(&t);                   /* 改频影响该 timer 上所有 PWM 通道 */
    }
    return 0;
}

#endif /* PLATFORM_ESP32 */
