/*
 * platform/esp32/platform_led.c — 板载状态灯实现（WS2812 @ GPIO48 + IO2 用户灯）
 *
 * 目的：BLE 测试期间禁开串口（DTR 下降沿会复位板子），状态灯就是替代
 * 串口日志的「可视化状态输出」，让用户不用接电脑也能判态。
 *
 * 硬件（实物板 = YD-ESP32-S3，已对照原理图与实物照片核实）：
 *   - WS2812B RGB，数据脚 GPIO48（U1，XL-5050RGBC-WS2812B）
 *   - IO2 单色用户 LED（GPIO2）；GPIO2 非 S3 strapping 脚（S3 的 strapping
 *     只有 0/3/45/46），固件无其他占用，仅烧录瞬间被自动下载电路接管。
 *
 * 亮度：WS2812(GPIO48) 极刺眼 → `LED_BRIGHT` 一降再降，最终 10/255（≈4%）；
 *       IO2 单色灯 GPIO 直驱满亮（不经 PWM，见下），要远距离可辨。
 *
 * ✅ **IO2 驱动方式 = GPIO 直驱，`QSHOU_LED2_ACTIVE_HIGH=1`（高电平点亮）**
 *   —— 2026-10-01 真机实测定案，此前两轮走的都是弯路：
 *   ① 第一轮以为「LED 调到 Working所以用 LEDC PWM」→ 结果 IO2 在 GPIO2 上
 *      **根本收不到 LEDC 波形**（pad 采样：duty 取 255/0/55 三档全部恒低），
 *      而同一根脚用 gpio_set_level 驱动时 pad 1/0 精确可控 → 弃用 LEDC 输出，
 *      详见下方 `LED2_DEFAULT_GPIO` 注释（含实测数据表）。
 *   ② 极性此前按「本板 TX/RX 灯必然低有效」的同源惯例猜 LOW —— **也是错的**：
 *      用户在 pad 恒高状态下肉眼确认 **IO2 常亮 → 高电平点亮**。
 *      教训：同源惯例只能当假设，必须以一次可控实验（拉高/拉低 + 眼看）钉死。
 *   `GET /api/v1/led?pol=0|1` 仍保留运行时翻转，供将来换板/换线时快速判极性。
 *
 * 状态灯协议（六态）：
 *   红常亮      启动故障（camera warm-up 失败等，platform_led_set_error）
 *   琥珀慢闪1Hz WiFi 连接中（拿不到 IP 停在此态 = 查凭据/信号）
 *   蓝慢闪 1Hz  WiFi 已连、BLE 广播中（搜不到设备时：亮 = 还在广播）
 *   绿常亮      BLE 已连接、HID 可用（键鼠无反应但灯绿 = 问题在 host 侧）
 *   青常亮      MJPEG 推流中（覆盖绿；停流自动回绿）
 *   白短闪80ms  HID 帧发射事件叠加（打字时白闪跟随 = 固件已发出）
 *   IO2 灯      BLE 已连接硬指示（**高电平点亮**，2026-10-01 实测钉死）
 *
 * 实现：独立 LED 任务 10Hz 轮询共享状态，**不碰 HID 热路径**（HID 发射
 * 只写时间戳，灯任务自己来读）。WS2812 用手写 RMT 字节编码器驱动
 * （10MHz 时钟，T0H=0.3µs/T0L=0.8µs，T1H=0.8µs/T1L=0.6µs，MSB 先行，
 * GRB 字节序），不引入 managed 组件、不改 sdkconfig。
 *
 * 编译：仅 PLATFORM_ESP32（见 main/CMakeLists.txt）。host 接缝有同名空桩。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"

#include <stdbool.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/rmt_tx.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"

/* 由 platform/esp32/platform_ble.c 提供：最近一次 HID 发射时刻（ms）。
 * 与 platform_net.c 的 HID 节流共用同一数据源，此处只读。 */
extern uint32_t platform_ble_last_tx_ms(void);

#define TAG                 "QSHOU_LED"
#define QSHOU_WS2812_GPIO    48      /* U1 WS2812B 数据脚（原理图 RGB_CTRL） */
#define QSHOU_LED2_GPIO      2       /* IO2 单色用户 LED */
#define QSHOU_LED2_ACTIVE_HIGH 1     /* ✅ 1=高电平点亮（2026-10-01 真机实测钉死：
                                     *    pad 恒高 → IO2 常亮。此前按「同板 TX/RX
                                     *    灯必然低有效」的惯例猜 0 是错的。） */
/* 以下三项只在 PWM 对照模式（`?mode=0`）下生效；默认 GPIO 直驱不分频，
 * 输出就是 pad 满幅高低电平（LED 亮度由板上串联电阻决定，实际观感合适）。 */
#define QSHOU_LED2_DUTY_ON   200     /* 点亮占空比 200/255（≈78%）：远距离要看得清，别太暗 */
#define QSHOU_LED2_PWM_HZ    5000    /* 5kHz，肉眼看不出闪烁 */
#define LED2_TIMER          LEDC_TIMER_1
#define LED2_CHANNEL        LEDC_CHANNEL_1
/* 🔴 **必须避开 LEDC_TIMER_0 / LEDC_CHANNEL_0**（2026-10-01 踩坑定案）：
 * esp_camera 用 LEDC 生成 OV2640 的 XCLK（platform_camera.c 里显式配置
 * `.ledc_timer=LEDC_TIMER_0, .ledc_channel=LEDC_CHANNEL_0`）。启动顺序是
 * 「LED 初始化 → 相机初始化」，若两队共用同一个 timer/channel，相机初始化会
 * 把该资源改写成 ~20MHz 的 XCLK 输出到 xclk 引脚，**GPIO2 的 PWM 输出就此被
 * 夺走** —— 这也是当年「启动会闪、稳态恒灭」的第一层原因。故 IO2 若走 PWM，
 * 必须单独占一组 TIMER_1/CHANNEL_1，与其它外设零冲突。 */
#define LED2_SPEED          LEDC_LOW_SPEED_MODE
#define LED2_RES            LEDC_TIMER_8_BIT   /* duty 取值 0..255 */
/* 🔴 **IO2 默认走 GPIO 直驱，不用 LEDC 输出**（2026-10-01 第二轮实测定案）：
 * 加了 pad 采样后才看清楚 ——  PWM 模式下 duty 取 255 / 0 / 55 三档，
 *   实测 pad_pct 全是 0（pad 恒低），连 duty=255（应恒高）都出不来；
 * 而 GPIO 直驱模式下 io2=1→pad_now=1、io2=0→pad_now=0，pad 完全可控。
 * 也就是说：**LEDC 根本没把波形打到 GPIO2 上**，这与「启动自检（GPIO 驱动）
 * 会闪、稳态（LEDC）永远不亮」的现象严丝合缝。
 * 项目里 LEDC 的唯一「确定占用」是相机 XCLK（TIMER_0/CHANNEL_0），已避开，
 * 仍不出波 —— 继续追 LEDC 内部原因收益很低。IO2 只是二值状态灯，不需要调光，
 * 于是干脆弃用 LEDC 输出（LEDC 仍初始化，仅给 `mode=0` 做对照排查）。
 * 副作用一并消掉：再也不会和任何外设抢 LEDC timer/channel。
 * 极性照旧用 `GET /api/v1/led?pol=0|1` 运行期翻转，判错不必重烧。 */
#define LED2_DEFAULT_GPIO   1       /* 1 = GPIO 直驱（默认），0 = LEDC PWM（对照） */
#define LED_TASK_STACK      2560
#define LED_TASK_PRIO       3       /* 低于 httpd(5)/gesture(5)/mjpeg(4)：纯显示 */
#define LED_TICK_MS         100     /* 10Hz 刷新 */
#define LED_BLINK_HALF_MS   500     /* 1Hz 慢闪的半周期 */
#define HID_FLASH_MS        80      /* HID 活动白闪的窗口 */

/* WS2812 亮度上限（0-255）：全彩灯珠极刺眼，用户实测定标 10/255（≈4%）。
 * 想再调只改这一行；琥珀态取 LED_BRIGHT/2、白闪取满 LED_BRIGHT，均随之缩放。
 * ⚠️ 10 已接近「暗处刚好可见」的下限，再低(<6)暗环境里几乎看不出状态。 */
#define LED_BRIGHT          10

static rmt_channel_handle_t s_rmt_chan = NULL;
static rmt_encoder_handle_t s_rmt_enc  = NULL;
static volatile bool s_streaming = false;
static volatile bool s_error     = false;
static volatile int  s_io2_manual = -1;   /* >=0 = 手工强制 duty（绕过极性换算）*/
static volatile int  s_pol_override = -1;/* >=0 = 运行时极性覆盖（1 高有效 / 0 低有效） */
static volatile int  s_io2_gpio = 0;     /* 1 = GPIO 直驱模式（诊断对照），0 = LEDC PWM */
static bool          s_gpio_active = false;/* GPIO 直驱是否已接管 pad */
static bool          s_ledc_ok   = false; /* LEDC 初始化是否成功（health 可读） */
static int           s_pad_high  = -1;    /* 自检：驱动高时读回的 pad 电平 */
static int           s_pad_low   = -1;    /* 自检：驱动低时读回的 pad 电平 */
static int           s_pad_now   = -1;    /* 最近一 tick 的 pad 实时电平 */
static int           s_pad_pct   = -1;    /* 最近一 tick 的 pad 高电平占比 0..100 */

static void led2_pad_input_enable(void);   /* 前向声明：模式切换要用到 */

void platform_led_set_streaming(bool on) { s_streaming = on; }
void platform_led_set_error(bool on)     { s_error = on; }

/* 手工强制 IO2 占空比（原始 duty，不做极性换算）：用于 GET /api/v1/led
 * 现场判极性 —— duty=255 时亮即高有效，duty=0 时亮即低有效。传 -1 恢复自动。 */
void platform_led_set_io2(int duty)
{
    if (duty < 0)         s_io2_manual = -1;
    else if (duty > 255)  s_io2_manual = 255;
    else                  s_io2_manual = duty;
}

/* 运行时覆盖极性：`pol>=0` 覆盖编译期宏，`-1` 恢复编译期值。
 * 存在意义：IO2 的极性肉眼只能判一次，错了不必重烧，打一个 URL 即可翻转
 * （GET /api/v1/led?pol=0|1）。设置后下一个 LED tick（≤100ms）生效。 */
void platform_led_set_io2_pol(int pol)
{
    if (pol < 0)    s_pol_override = -1;
    else            s_pol_override = pol ? 1 : 0;
}

/* 运行时切换 IO2 驱动方式：`gpio_mode=1` 用 GPIO 直驱，`0` 回到 LEDC PWM。
 * 排查「怎么翻极性都不亮」时这是决定性对照：
 *   GPIO 模式能亮 + PWM 模式不亮 → LEDC 侧问题（资源被抢/时钟/pad 路由），与极性无关；
 *   GPIO 模式也不亮 → pad 或 LED 本身的问题（极性猜错、LED 不在这根脚）。
 * 切换必须先把 LEDC 停掉并释放 pad，否则两种输出会在同一根 pin 上打架。 */
void platform_led_set_io2_mode(int gpio_mode)
{
    int want = gpio_mode ? 1 : 0;
    if (want == s_io2_gpio)
        return;
    s_io2_gpio = want;
    if (want) {
        if (s_ledc_ok) ledc_stop(LED2_SPEED, LED2_CHANNEL, 0);
        gpio_reset_pin(QSHOU_LED2_GPIO);
        gpio_config_t io = {
            .pin_bit_mask = 1ULL << QSHOU_LED2_GPIO,
            .mode         = GPIO_MODE_INPUT_OUTPUT,
            .pull_up_en   = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type    = GPIO_INTR_DISABLE,
        };
        gpio_config(&io);
        s_gpio_active = true;
    } else {
        if (s_gpio_active) {
            gpio_reset_pin(QSHOU_LED2_GPIO);
            s_gpio_active = false;
        }
        /* LEDC 通道配置仍然生效，只需把信号重新绑回这根 pad */
        if (s_ledc_ok)
            ledc_set_pin(QSHOU_LED2_GPIO, LED2_SPEED, LED2_CHANNEL);
        led2_pad_input_enable();
    }
}

int platform_led_io2_mode_get(void) { return s_io2_gpio ? 1 : 0; }

/* 回读 LEDC 初始化结果 + pad 电平（-1=未测）。
 * pad_now/pad_pct 由 LED 任务每 tick 更新，是本轮的**客观判据** —— 肉眼分不清
 * 「暗」和「完全不亮」，但设定 duty 与 pad 实测占空比必须能对得上：
 *   duty=255 却 pad_pct≈0、duty=0 却 pad_pct≈100 → 量反了（极性或换算错）；
 *   duty=55 却 pad_pct 恒等于 0 或 100（不是 ~22） → LEDC 根本没在 pad 上输出；
 *   pad_now 恒为 -1/同一值且灯不亮 → pad 被外部拉死或 LED 不在这根脚。 */
void platform_led_io2_diag(int *ready, int *pad_high, int *pad_low,
                           int *pad_now, int *pad_pct)
{
    if (ready)    *ready    = s_ledc_ok ? 1 : 0;
    if (pad_high) *pad_high = s_pad_high;
    if (pad_low)  *pad_low  = s_pad_low;
    if (pad_now)  *pad_now  = s_pad_now;
    if (pad_pct)  *pad_pct  = s_pad_pct;
}

/* ---- WS2812 驱动（RMT bytes encoder） ---- */
static bool ws2812_init(void)
{
    rmt_tx_channel_config_t tx_cfg = {
        .clk_src           = RMT_CLK_SRC_DEFAULT,
        .gpio_num          = QSHOU_WS2812_GPIO,
        .mem_block_symbols = 64,               /* 一帧仅 24 符号，64 富余 */
        .resolution_hz     = 10 * 1000 * 1000, /* 100ns/刻度 */
        .trans_queue_depth = 4,
    };
    if (rmt_new_tx_channel(&tx_cfg, &s_rmt_chan) != ESP_OK)
        return false;

    /* WS2812 归零码：T0H=0.3µs/T0L=0.8µs，T1H=0.8µs/T1L=0.6µs（10MHz 下 3/8、8/6 刻度） */
    rmt_bytes_encoder_config_t enc_cfg = {
        .bit0 = { .duration0 = 3, .level0 = 1, .duration1 = 8, .level1 = 0 },
        .bit1 = { .duration0 = 8, .level0 = 1, .duration1 = 6, .level1 = 0 },
        .flags.msb_first = 1,
    };
    if (rmt_new_bytes_encoder(&enc_cfg, &s_rmt_enc) != ESP_OK)
        return false;
    if (rmt_enable(s_rmt_chan) != ESP_OK)
        return false;
    return true;
}

/* 写一个颜色（WS2812 字节序为 GRB，MSB 先行） */
static void ws2812_set(uint8_t g, uint8_t r, uint8_t b)
{
    if (!s_rmt_chan) return;
    uint8_t grb[3] = { g, r, b };
    rmt_transmit_config_t tx_cfg = { .loop_count = 0 };
    if (rmt_transmit(s_rmt_chan, s_rmt_enc, grb, sizeof grb, &tx_cfg) == ESP_OK)
        rmt_tx_wait_all_done(s_rmt_chan, -1);
}

/* ---- IO2 用户灯（默认 GPIO 直驱，LEDC 仅留作对照） ---- */

/* LEDC 只配了输出，**不开输入**，gpio_get_level 就会读到浮空/无效值，诊断
 * 时无法确认 PWM 是否真落到 pad 上。这里单独补一次 input enable（只动 pad
 * 方向寄存器，不碰 GPIO matrix 的信号路由，因此不会掐掉 LEDC 输出）。 */
static void led2_pad_input_enable(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << QSHOU_LED2_GPIO,
        .mode         = GPIO_MODE_INPUT_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
}

/* 连采 64 次估算 pad 高电平占比（0..100）。
 * 采样跨度约 64×3µs ≈ 190µs，覆盖 5kHz PWM 的整整一个周期（200µs），
 * 因此 duty=55/255 时应读到约 21%，duty=255 → 100%，duty=0 → 0%。
 * 与设定值对不上，就说明 pad 上的波形不是我们想要的那个。 */
static int led2_pad_sample_pct(void)
{
    int hi = 0;
    for (int i = 0; i < 64; i++) {
        if (gpio_get_level(QSHOU_LED2_GPIO)) hi++;
        esp_rom_delay_us(3);
    }
    return (hi * 100) / 64;
}

static bool led2_init(void)
{
    ledc_timer_config_t t = {
        .speed_mode      = LED2_SPEED,
        .timer_num       = LED2_TIMER,
        .duty_resolution = LED2_RES,
        .freq_hz         = QSHOU_LED2_PWM_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    if (ledc_timer_config(&t) != ESP_OK)
        return false;

    ledc_channel_config_t c = {
        .gpio_num   = QSHOU_LED2_GPIO,
        .speed_mode = LED2_SPEED,
        .channel    = LED2_CHANNEL,
        .timer_sel  = LED2_TIMER,
        .duty       = 0,            /* 初始熄灭 */
        .hpoint     = 0,
    };
    if (ledc_channel_config(&c) != ESP_OK)
        return false;
    led2_pad_input_enable();   /* 补开输入：health 要能读回 pad 真实电平 */
    return true;
}

/* 按「有效极性」算出 duty 并下发：
 *   高有效(pol=1)：on → duty=200（pad 高占 78%）；off → duty=0
 *   低有效(pol=0)：on → duty=255-200=55（pad 低占 78%）；off → duty=255
 * hpoint=0 即相位不偏移。极性优先取运行时覆盖值。 */
static void led2_set(bool on)
{
    int pol = (s_pol_override >= 0) ? (int)s_pol_override : QSHOU_LED2_ACTIVE_HIGH;
#if QSHOU_LED2_ACTIVE_HIGH
    uint32_t hi = QSHOU_LED2_DUTY_ON, lo = 0u;
#else
    uint32_t hi = 255u - QSHOU_LED2_DUTY_ON, lo = 255u;
#endif
    if (QSHOU_LED2_ACTIVE_HIGH != pol) {          /* 运行时与编译期不同 → 对调 */
        uint32_t t = hi; hi = lo; lo = t;
    }
    ledc_set_duty_and_update(LED2_SPEED, LED2_CHANNEL, on ? hi : lo, 0);
}

/* ---- IO2 引脚自检（启动跑一次，约 1.2s）----
 * 起因：用户反馈 IO2 灯从未点亮。可能原因有三，肉眼无法区分：
 *   ① 极性相反（低有效）——改宏即可；
 *   ② GPIO 输出根本没落到 pad 上（被自动下载电路/三极管拉死）——软件救不了；
 *   ③ 那颗丝印 IO2 的 LED 压根没接在 GPIO2 上。
 * 用 INPUT_OUTPUT 模式：既能驱动、又能读回 pad 真实电平，把三种情况钉死：
 *   pad_high=1 且 pad_low=0 → GPIO 确实在控制 pad（看灯在哪一相亮即可判极性）
 *   pad_high=pad_low=0      → pad 被外部拉死在低电平，IO2 不可用
 *   pad_high=pad_low=1      → pad 被外部拉死在高电平，IO2 不可用
 * 驱动节奏刻意做成不对称波形（高段是低段的 2 倍长），万一再来一次「不亮」，
 * 用户只需答一句「长的那段亮还是短的那段亮」，就能当场判出极性。
 * ⚠️ 本函数是阻塞的：每轮 600ms × 2 ≈ 1.2s 启动延迟。曾为 4s（1.2s/0.8s×2），
 *    极性查明后压缩；只在启动跑一次，只当作 line-level 体检。 */
static void io2_selftest(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << QSHOU_LED2_GPIO,
        .mode         = GPIO_MODE_INPUT_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    for (int i = 0; i < 2; i++) {
        gpio_set_level(QSHOU_LED2_GPIO, 1);
        vTaskDelay(pdMS_TO_TICKS(400));      /* 高段：较长 */
        s_pad_high = gpio_get_level(QSHOU_LED2_GPIO);
        gpio_set_level(QSHOU_LED2_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(200));      /* 低段：较短 */
        s_pad_low = gpio_get_level(QSHOU_LED2_GPIO);
    }
    gpio_set_level(QSHOU_LED2_GPIO, 0);
    gpio_reset_pin(QSHOU_LED2_GPIO);   /* 释放 pad，交给 LEDC 重新接管 */
}

/* IO2 输出统一入口：`on` = 「希望灯亮」，两种驱动方式各自按有效极性换算。
 *   GPIO 直驱：pol=1 → 亮=高电平；pol=0 → 亮=低电平。
 *   LEDC PWM ：走 led2_set 的 duty 换算（仅作对照，见 LED2_DEFAULT_GPIO）。
 * manual 值在 GPIO 模式下按 >0 判「亮」，在 PWM 模式下按原始 duty 下发。 */
static void io2_apply(bool on)
{
    if (s_io2_gpio) {
        int pol = (s_pol_override >= 0) ? (int)s_pol_override : QSHOU_LED2_ACTIVE_HIGH;
        int lvl = pol ? (on ? 1 : 0) : (on ? 0 : 1);
        gpio_set_level(QSHOU_LED2_GPIO, lvl);
        s_pad_now = gpio_get_level(QSHOU_LED2_GPIO);
        s_pad_pct = s_pad_now ? 100 : 0;
        return;
    }
    if (s_io2_manual >= 0)
        ledc_set_duty_and_update(LED2_SPEED, LED2_CHANNEL, (uint32_t)s_io2_manual, 0);
    else
        led2_set(on);
    s_pad_pct = led2_pad_sample_pct();
    s_pad_now = gpio_get_level(QSHOU_LED2_GPIO);
}

/* ---- 状态判定 + 刷新任务（10Hz） ---- */
static void led_task(void *arg)
{
    (void)arg;
    uint32_t tick = 0;
    for (;;) {
        tick++;

        bool wifi = platform_wifi_is_connected();
        bool ble  = platform_ble_is_connected();

        /* HID 活动：仅当 BLE 已连接时判窗口（未连接时 last_tx_ms 无意义） */
        bool hid_flash = false;
        if (ble) {
            uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            hid_flash = (uint32_t)(now - platform_ble_last_tx_ms()) < HID_FLASH_MS;
        }

        uint8_t g = 0, r = 0, b = 0;
        if (s_error) {
            r = LED_BRIGHT;                              /* 红：启动故障 */
        } else if (!wifi) {
            if (((tick * LED_TICK_MS) / LED_BLINK_HALF_MS) & 1) {
                r = LED_BRIGHT; g = LED_BRIGHT / 2;      /* 琥珀慢闪：WiFi 连接中 */
            }
        } else if (!ble) {
            if (((tick * LED_TICK_MS) / LED_BLINK_HALF_MS) & 1) {
                b = LED_BRIGHT;                          /* 蓝慢闪：BLE 广播中 */
            }
        } else if (s_streaming) {
            g = LED_BRIGHT; b = LED_BRIGHT;              /* 青：推流中 */
        } else {
            g = LED_BRIGHT;                              /* 绿：BLE 已连接空闲 */
        }
        if (!s_error && hid_flash) {
            g = r = b = LED_BRIGHT;                      /* 白短闪：HID 活动（最高显示优先） */
        }

        ws2812_set(g, r, b);

        /* IO2 灯 = BLE 已连接硬指示（二值、远距离可辨，与 WS2812 双通道确认）。
         * 三种取值情形：
         *   - /api/v1/led 设了 io2=<值>：GPIO 模式下 >0 判亮，PWM 模式下按原始 duty；
         *   - 否则：跟随 BLE 连接状态，由 io2_apply 按有效极性换算输出。
         * 每 tick 末尾都补一次 pad 采样：health 的 io2_pad_now/pad_pct 就是这个值，
         * 用来验证「固件想输出什么」和「pad 上实际是什么」是否一致。 */
        bool io2_on = (s_io2_manual >= 0) ? (s_io2_manual > 0) : ble;
        io2_apply(io2_on);

        vTaskDelay(pdMS_TO_TICKS(LED_TICK_MS));
    }
}

void platform_led_init(void)
{
    /* 先做 GPIO2 pad 自检（判「极性」还是「pad 被外部拉死」），再交给 LEDC。
     * 自检约 1.2s，只在启动时一次。 */
    io2_selftest();

    /* IO2 用户灯：先建 LEDC 通道（失败不影响 GPIO 直驱），随后按 LED2_DEFAULT_GPIO
     * 决定是否切到 GPIO 直驱 —— 默认**不靠 LEDC 输出**，见 LED2_DEFAULT_GPIO 注释。 */
    s_ledc_ok = led2_init();
    if (!s_ledc_ok)
        ESP_LOGW(TAG, "IO2 LEDC init failed (GPIO direct drive unaffected)");
#if LED2_DEFAULT_GPIO
    platform_led_set_io2_mode(1);
#endif

    if (!ws2812_init())
        ESP_LOGW(TAG, "WS2812 RMT init failed; status LED disabled (IO2 LED still works)");

    if (xTaskCreate(led_task, "qshou_led", LED_TASK_STACK, NULL, LED_TASK_PRIO, NULL) != pdPASS)
        ESP_LOGE(TAG, "led task create failed");
    else
        ESP_LOGI(TAG, "status LED up: WS2812@gpio%d bright=%d + IO2@gpio%d pwm duty %d/255 "
                      "(pad high=%d low=%d, 10Hz)",
                 QSHOU_WS2812_GPIO, LED_BRIGHT, QSHOU_LED2_GPIO, QSHOU_LED2_DUTY_ON,
                 s_pad_high, s_pad_low);
}

#endif /* PLATFORM_ESP32 */
