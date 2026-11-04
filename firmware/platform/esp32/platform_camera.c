/*
 * platform/esp32/platform_camera.c — ESP32 相机接缝实现（esp32-camera 路线）
 *
 * 组件：esp32-camera（Espressif 托管组件，Apache-2.0）
 *   获取（到货后、联网环境下）：
 *       idf.py add-dependency "espressif/esp32-camera^2.0.0"
 *   或写入 firmware/main/idf_component.yml。
 *   本文件在缺少该组件时**不会**参与 host 构建，因此不影响零硬件流程。
 *
 * 2026-10-25：A 板摄像头由 OV2640 更换为 OV5640（5MP）。驱动自动探测 PID，
 *   逻辑零改动即可出帧；本文件新增两项 OV5640 适配：
 *   ① framesize 上限 sensor 感知（旧写死 13 挡住 1080p）；
 *   ② 空闲 standby 降温（0x3008[6]，见 QSHOU_CAM_IDLE_SLEEP_S 注释块）。
 *   OV2640 行为完全保持原样（含 UXGA 上限与持续运转）。
 *
 * 接缝契约（platform.h）：
 *   platform_camera_capture 返回的 *buf 必须**在下次调用前保持有效**。
 *   但 esp_camera_fb_get() 返回的 frame buffer 归还后指针失效，
 *   因此这里做一次拷贝到内部双缓冲（VGA 单帧 JPEG 约 20–60KB）。
 *
 * 引脚护栏（D 档）：所有 GPIO 集中在本文件顶部宏，且**允许编译期覆写**
 *   （-DQSHOU_CAM_PIN_XCLK=... ），到货核对板型后只需改宏或加编译定义，
 *   不触碰任何逻辑代码，避免返工。
 *   参考分配见 docs/partition_pin_plan.md 第 2 节（ESP32-S3-EYE）。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"

#include <string.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_camera.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "platform/platform_camera.h"

#define TAG "QSHOU_CAM"

/* ── OV2640 DVP 引脚（参考 ESP32-S3-EYE；到货后按实际板型核对）──
 * 均可用 -D 覆写，避免硬编码锁死（D 档护栏）。 */
#ifndef QSHOU_CAM_PIN_XCLK
#define QSHOU_CAM_PIN_XCLK   15
#endif
#ifndef QSHOU_CAM_PIN_PCLK
#define QSHOU_CAM_PIN_PCLK   13
#endif
#ifndef QSHOU_CAM_PIN_VSYNC
#define QSHOU_CAM_PIN_VSYNC   6
#endif
#ifndef QSHOU_CAM_PIN_HREF
#define QSHOU_CAM_PIN_HREF    7
#endif
#ifndef QSHOU_CAM_PIN_SIOD            /* SCCB SDA */
#define QSHOU_CAM_PIN_SIOD    4
#endif
#ifndef QSHOU_CAM_PIN_SIOC            /* SCCB SCL */
#define QSHOU_CAM_PIN_SIOC    5
#endif
/* 数据总线 D0–D7 */
#ifndef QSHOU_CAM_PIN_D0
#define QSHOU_CAM_PIN_D0     11
#endif
#ifndef QSHOU_CAM_PIN_D1
#define QSHOU_CAM_PIN_D1      9
#endif
#ifndef QSHOU_CAM_PIN_D2
#define QSHOU_CAM_PIN_D2      8
#endif
#ifndef QSHOU_CAM_PIN_D3
#define QSHOU_CAM_PIN_D3     10
#endif
#ifndef QSHOU_CAM_PIN_D4
#define QSHOU_CAM_PIN_D4     12
#endif
#ifndef QSHOU_CAM_PIN_D5
#define QSHOU_CAM_PIN_D5     18
#endif
#ifndef QSHOU_CAM_PIN_D6
#define QSHOU_CAM_PIN_D6     17
#endif
#ifndef QSHOU_CAM_PIN_D7
#define QSHOU_CAM_PIN_D7     16
#endif
/* 电源/复位控制脚（-1 = 不使用 / 悬空） */
#ifndef QSHOU_CAM_PIN_PWDN
#define QSHOU_CAM_PIN_PWDN   -1
#endif
#ifndef QSHOU_CAM_PIN_RESET
#define QSHOU_CAM_PIN_RESET  -1
#endif
#ifndef QSHOU_CAM_XCLK_FREQ_HZ
#define QSHOU_CAM_XCLK_FREQ_HZ 20000000   /* 20MHz，OV2640 常用值 */
#endif

/* ── 输出格式与分辨率 ──
 * JPEG 输出：相机模组内部完成 JPEG 压缩，固件只搬运（省 CPU，适配 MJPEG 图传）。
 * 分辨率默认 VGA(640×480)：quality=16 下单帧约 15–35KB，保分辨率优先（后续要做屏幕识别与
 * 定位精度，清晰度是硬指标）。
 * 注：quality 曾在恶劣 RF 环境（金属板贴设备、RSSI 差）下调到 30 以缩体积缓解 HID 抖动；
 *     RF 修复后（RSSI -43）+ 推流已独立于 httpd + 8fps + HID 活动节流，射频预算有余量，
 *     故回调到 16 恢复清晰度，分辨率仍保持 VGA 不动（见规格 video 章节）。
 * ⚠️ framesize 枚举：当前 esp32-camera 组件为标准枚举（FRAMESIZE_VGA=10=640×480，
 *    与 driver/sensor.c 的 resolution[] 表逐行对应；早期版本曾错位 2 档，勿沿用旧结论）。
 *    实测真值表（枚举→像素）：6=320×240、9=480×320、10=640×480、11=800×600、12=1024×768、
 *    13=1280×720、15=1600×1200、16=1920×1080。
 *    下拉框 ui.html 与 /api/v1/camera frame_size 均按此真值表工作，运行时切换免重烧。
 *    上限为 sensor 感知（见 camera_max_framesize），OV5640 可用到 16（1080p）。 */
#ifndef QSHOU_CAM_FRAMESIZE
#define QSHOU_CAM_FRAMESIZE  10   /* 真正的 VGA 640×480（SDK 宏 FRAMESIZE_VGA=8 在此构建下是 400×296） */
#endif
#ifndef QSHOU_CAM_JPEG_QUALITY
#define QSHOU_CAM_JPEG_QUALITY  16    /* 0–63，越小越清晰、体积越大；16=RF 修复后回调，保 VGA 分辨率兼顾清晰度 */
#endif
#ifndef QSHOU_CAM_FB_COUNT
#define QSHOU_CAM_FB_COUNT      2     /* 驱动层 frame buffer 数量 */
#endif

/* 内部拷贝缓冲容量：单帧 JPEG 上限。需覆盖最高可选分辨率 FHD(1920×1080) 的 JPEG，
 * 复杂场景下单帧可能 ~200–350KB；留足余量防截断（放 PSRAM，8MB 足够）。 */
#ifndef QSHOU_CAM_BUF_CAP
#define QSHOU_CAM_BUF_CAP  (512 * 1024)
#endif

/* ── 高分辨率实用上限（OV5640 专用）──────────────────────────────
 * camera_sensor[] 表里 OV5640 max_size=QSXGA(23)/5MP，但本设计的缓冲是硬约束：
 *   s_buf(QSHOU_CAM_BUF_CAP 512KB) + MJPEG 组帧缓冲(STREAM_PART_CAP 512KB+512)。
 * 1080p JPEG(q16) 约 200–350KB 可容纳；QXGA(2048×1536) 以上可能溢出被丢帧。
 * 故 runtime 上限 = min(sensor 表上限, 本宏)。OV2640 表上限 UXGA(15) 不受影响。 */
#ifndef QSHOU_CAM_MAX_FS
#define QSHOU_CAM_MAX_FS  16   /* FHD 1920×1080 */
#endif

/* ── OV5640 空闲 standby（降温）────────────────────────────────
 * 现象：OV5640 是 5MP sensor，ISP+JPEG 引擎持续运转，模组温度明显高于 OV2640
 *   （2026-10-25 用户实测「发热有点大」）。
 * esp32-camera 驱动不暴露 standby，但 OV5640/OV3660 共用 0x3008[6] standby 位：
 *   espressif/esp32-camera#672 实测 OV3660 进 standby 电流降约 60mA（深睡场景 <5mA），
 *   issue 作者确认「OV5640 has the same registers」。写入方式：
 *     进 standby: sensor->set_reg(s, 0x3008, 0x40, 0x40)
 *     唤醒:      sensor->set_reg(s, 0x3008, 0x40, 0x00)
 * ⚠️ OV2640 的 standby（0x109[4]）唤醒是社区已知硬件缺陷（同 issue 多人复现）
 *   → 本路径只对 PID==OV5640_PID 生效，其它 sensor 保持原状持续运转。
 * standby 不影响已配置寄存器（不进复位），唤醒后从头几帧起恢复出帧。 */
#ifndef QSHOU_CAM_IDLE_SLEEP_S
#define QSHOU_CAM_IDLE_SLEEP_S  60    /* 空闲多少秒后进 standby；0=禁用 */
#endif
#ifndef QSHOU_CAM_IDLE_CHECK_MS
#define QSHOU_CAM_IDLE_CHECK_MS  10000 /* 空闲巡检周期 */
#endif
#ifndef QSHOU_CAM_WAKE_DISCARD
#define QSHOU_CAM_WAKE_DISCARD  5     /* 唤醒后丢弃的头帧数（传感器重稳定） */
#endif
#ifndef QSHOU_CAM_IDLE_TASK_STACK
#define QSHOU_CAM_IDLE_TASK_STACK  2560
#endif
#ifndef QSHOU_CAM_IDLE_TASK_PRIO
#define QSHOU_CAM_IDLE_TASK_PRIO  3    /* 低于 gesture(5)/httpd(5)/mjpeg(4) */
#endif

static bool     s_inited = false;
static uint8_t *s_buf    = NULL;      /* 双缓冲：指向当前有效帧（PSRAM 分配） */
static size_t   s_len    = 0;
static camera_config_t s_cfg;         /* ensure_init 用的配置：切档经 esp_camera_reconfigure 复用 */

/* 相机参数（传感器级，真实生效，非预览滤镜）：
 *   exposure_auto   : 1=自动曝光(AEC on)；0=手动(用 exposure_value)
 *   exposure_value  : 手动曝光值，OV2640 约 0..1200
 *   brightness/contrast/saturation : -2..2（传感器 DSP，符号级）
 * esp_camera_init 会把传感器重置为默认，故必须在 init 之后重新套用（见 camera_apply_settings）。 */
static int s_exp_auto   = 1;
static int s_aec_value  = 0;
static int s_brightness = 0;
static int s_contrast   = 0;
static int s_saturation = 0;
static int s_auto_wb    = 1;   /* AWB（自动白平衡）开关 */
static int s_auto_gain  = 1;   /* AGC（自动增益）开关 */
static int s_framesize  = QSHOU_CAM_FRAMESIZE;  /* 当前分辨率枚举值（默认编译期 VGA） */

/* 把已保存的相机参数套用到传感器。传感器函数指针可能为 NULL，逐个防御。 */
static void camera_apply_settings(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s)
        return;
    if (s->set_exposure_ctrl)
        s->set_exposure_ctrl(s, s_exp_auto ? 1 : 0);
    if (!s_exp_auto && s->set_aec_value)
        s->set_aec_value(s, s_aec_value);
    if (s->set_aec2)               /* AEC 数字增益 DSP，常开 */
        s->set_aec2(s, 1);
    if (s->set_brightness)
        s->set_brightness(s, s_brightness);
    if (s->set_contrast)
        s->set_contrast(s, s_contrast);
    if (s->set_saturation)
        s->set_saturation(s, s_saturation);
    /* 自动功能开关：与 AEC/AWB 增益一样，逐个防御 NULL（不同 sensor 接线不同）。
     * set_whitebal = AWB 自动白平衡；set_gain_ctrl = AGC 自动增益（名称来自 ov2640.c，
     * 不是 set_gainctrl）。 */
    if (s->set_whitebal)
        s->set_whitebal(s, s_auto_wb ? 1 : 0);
    if (s->set_gain_ctrl)
        s->set_gain_ctrl(s, s_auto_gain ? 1 : 0);
}

/* ── 互斥锁与 standby 状态 ──
 * 锁存在的理由：/snapshot（httpd 任务）与 /stream（mjpeg 任务）都会进 capture，
 * 此前两者并发会同时踩 s_buf/s_len（偶发花屏帧）；standby 状态转换也必须与
 * capture 互斥，否则「巡检决定睡」与「正在取帧」交叠会毁掉进行中的那一帧。
 * 所有公开 setter 与 capture 都先拿锁；camera_apply_settings 约定**调用方持锁**。 */
static SemaphoreHandle_t s_cam_mutex;
static bool              s_standby = false;        /* OV5640 是否处于 standby */
static int64_t           s_last_capture_us = 0;    /* 最近一次成功取帧（esp_timer 单调钟） */

/* 传感器当前支持的最大 framesize（sensor 表 max_size 与缓冲实用上限取小）。
 * 未初始化 / 查不到时退回编译期实用上限。 */
static int camera_max_framesize(void)
{
    int max = QSHOU_CAM_MAX_FS;
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
        if (info && (int)info->max_size < max)
            max = (int)info->max_size;
    }
    return max;
}

/* 进/出 standby。约定：调用方持有 s_cam_mutex。仅 OV5640 生效（见宏块注释）。 */
static void camera_set_standby_locked(int enable)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s || !s->set_reg)
        return;
    if (s->id.PID != OV5640_PID)      /* OV2640 唤醒缺陷，不碰 */
        return;
    if ((enable != 0) == s_standby)
        return;                        /* 状态已一致，免重复写 */
    int rc = s->set_reg(s, 0x3008, 0x40, enable ? 0x40 : 0x00);
    if (rc == 0) {
        s_standby = (enable != 0);
        ESP_LOGI(TAG, "OV5640 %s (0x3008[6])",
                 s_standby ? "standby：空闲降温" : "wake：恢复出帧");
    } else {
        ESP_LOGW(TAG, "standby write failed rc=%d", rc);
    }
}

/* 唤醒并丢弃头几帧（唤醒后传感器重稳定，头帧可能不完整/花屏）。
 * 约定：调用方持有 s_cam_mutex。 */
static void camera_wake_locked(void)
{
    if (!s_standby)
        return;
    camera_set_standby_locked(0);
    for (int i = 0; i < QSHOU_CAM_WAKE_DISCARD; i++) {
        camera_fb_t *f = esp_camera_fb_get();   /* 有界超时，不会死等 */
        if (!f)
            break;
        esp_camera_fb_return(f);
    }
}

/* 空闲巡检：超过阈值无取帧即进 standby。传感器未初始化时什么都不做。 */
static void cam_idle_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(QSHOU_CAM_IDLE_CHECK_MS));
        if (!s_inited || !s_cam_mutex)
            continue;
        if (QSHOU_CAM_IDLE_SLEEP_S == 0)
            continue;
        if (xSemaphoreTake(s_cam_mutex, pdMS_TO_TICKS(500)) != pdTRUE)
            continue;
        int64_t idle_us = esp_timer_get_time() - s_last_capture_us;
        bool should_sleep = (idle_us > (int64_t)QSHOU_CAM_IDLE_SLEEP_S * 1000000);
        if (should_sleep)
            camera_set_standby_locked(1);
        xSemaphoreGive(s_cam_mutex);
    }
}

/* 参数 setter 统一入口：拿锁 → 唤醒（standby 中先醒再写寄存器）→ apply → 放锁。
 * 未初始化（无锁）时退化为直接套用 —— 彼时也无 standby 可言。 */
static void camera_apply_settings_locked_entry(void)
{
    if (!s_cam_mutex) {
        camera_apply_settings();
        return;
    }
    if (xSemaphoreTake(s_cam_mutex, portMAX_DELAY) != pdTRUE) {
        camera_apply_settings();
        return;
    }
    camera_wake_locked();
    camera_apply_settings();
    xSemaphoreGive(s_cam_mutex);
}

/* ── 初始化（幂等）──
 * 由 platform_stream_start 或首次 capture 时触发；也可在 platform_system_init 后
 * 显式调用。放在本文件是因为只有 camera 接缝知道自己的引脚与格式。 */
static int camera_ensure_init(void)
{
    if (s_inited)
        return 0;

    /* 锁最先建：其后所有传感器操作（含 apply_settings）都约定持锁 */
    if (!s_cam_mutex) {
        s_cam_mutex = xSemaphoreCreateMutex();
        if (!s_cam_mutex) {
            ESP_LOGE(TAG, "camera mutex create failed");
            return -1;
        }
    }

    if (!s_buf) {
        /* 帧缓冲放 PSRAM（8MB Octal）：内部 SRAM 不足以放 128KB 拷贝缓冲 */
        s_buf = (uint8_t *)heap_caps_malloc(QSHOU_CAM_BUF_CAP, MALLOC_CAP_SPIRAM);
        if (!s_buf) {
            /* 退化到内部 RAM（可能失败，视余量） */
            s_buf = (uint8_t *)malloc(QSHOU_CAM_BUF_CAP);
        }
        if (!s_buf) {
            ESP_LOGE(TAG, "alloc frame copy buffer (%d bytes) failed",
                     (int)QSHOU_CAM_BUF_CAP);
            return -1;
        }
    }

    /* 分辨率 / 质量：NVS qshou_cfg 优先（冷生效），缺省用编译期值。
     * 运行时热切换仍走 /api/v1/camera（platform_camera_set_framesize 等）。 */
    uint8_t b = 0;
    int fs = QSHOU_CAM_FRAMESIZE, q = QSHOU_CAM_JPEG_QUALITY;
    if (platform_cfg_get_u8("cam_framesize", &b) && b) { fs = b; s_framesize = b; }
    if (platform_cfg_get_u8("cam_quality", &b))         { q = b; }

    camera_config_t cfg = {
        .pin_pwdn     = QSHOU_CAM_PIN_PWDN,
        .pin_reset    = QSHOU_CAM_PIN_RESET,
        .pin_xclk     = QSHOU_CAM_PIN_XCLK,
        .pin_sccb_sda = QSHOU_CAM_PIN_SIOD,
        .pin_sccb_scl = QSHOU_CAM_PIN_SIOC,
        .pin_d7       = QSHOU_CAM_PIN_D7,
        .pin_d6       = QSHOU_CAM_PIN_D6,
        .pin_d5       = QSHOU_CAM_PIN_D5,
        .pin_d4       = QSHOU_CAM_PIN_D4,
        .pin_d3       = QSHOU_CAM_PIN_D3,
        .pin_d2       = QSHOU_CAM_PIN_D2,
        .pin_d1       = QSHOU_CAM_PIN_D1,
        .pin_d0       = QSHOU_CAM_PIN_D0,
        .pin_vsync    = QSHOU_CAM_PIN_VSYNC,
        .pin_href     = QSHOU_CAM_PIN_HREF,
        .pin_pclk     = QSHOU_CAM_PIN_PCLK,

        .xclk_freq_hz = QSHOU_CAM_XCLK_FREQ_HZ,
        .ledc_timer   = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,

        .pixel_format = PIXFORMAT_JPEG,
        .frame_size   = fs,
        .jpeg_quality = q,
        .fb_count     = QSHOU_CAM_FB_COUNT,
        /* 帧缓冲放 PSRAM：VGA JPEG 双缓冲需要较大连续空间 */
        .fb_location  = CAMERA_FB_IN_PSRAM,
        .grab_mode    = CAMERA_GRAB_LATEST,   /* 图传要「最新帧」，不等旧帧 */
    };

    s_cfg = cfg;   /* 保存配置：运行时切档（platform_camera_set_framesize）经 reconfigure 复用 */
    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init failed: %s", esp_err_to_name(err));
        return -1;
    }
    s_inited = true;
    /* apply_settings 约定持锁（见 s_cam_mutex 注释）；init 期间无并发，但契约统一 */
    xSemaphoreTake(s_cam_mutex, portMAX_DELAY);
    camera_apply_settings();   /* esp_camera_init 已把传感器重置为默认，此处重新套用保存的参数 */
    xSemaphoreGive(s_cam_mutex);
    /* 日志按实际探测到的传感器打印（A 板现为 OV5640，历史固件为 OV2640），
     * 避免硬编码型号在换 sensor 后误导排查（PID 常量来自 esp32-camera sensor.h）。 */
    sensor_t *s = esp_camera_sensor_get();
    const char *sensor_name = "camera";
    if (s) {
        switch (s->id.PID) {
        case OV2640_PID: sensor_name = "OV2640"; break;
        case OV5640_PID: sensor_name = "OV5640"; break;
        default:         sensor_name = "camera"; break;
        }
    }
    ESP_LOGI(TAG, "%s ready (PID=0x%04x, DVP, JPEG, xclk=%dHz, pins X=%d P=%d V=%d H=%d)",
             sensor_name, s ? (unsigned)s->id.PID : 0,
             (int)QSHOU_CAM_XCLK_FREQ_HZ,
             (int)QSHOU_CAM_PIN_XCLK, (int)QSHOU_CAM_PIN_PCLK,
             (int)QSHOU_CAM_PIN_VSYNC, (int)QSHOU_CAM_PIN_HREF);
    s_last_capture_us = esp_timer_get_time();   /* 预热帧即算活动，避免起来就睡 */
    /* 空闲巡检任务：OV5640 降温用；OV2640 下 camera_set_standby_locked 直接 no-op */
    BaseType_t ok = xTaskCreate(cam_idle_task, "cam_idle",
                                QSHOU_CAM_IDLE_TASK_STACK, NULL,
                                QSHOU_CAM_IDLE_TASK_PRIO, NULL);
    if (ok != pdPASS)
        ESP_LOGW(TAG, "cam_idle task create failed (idle standby disabled)");
    return 0;
}

int platform_camera_capture(const uint8_t **buf, size_t *len)
{
    if (!buf || !len)
        return -1;

    if (camera_ensure_init() != 0)
        return -1;

    /* 锁内做三件事：① standby 中先唤醒并丢头帧；② 独占 s_buf/s_len（此前
     * /snapshot 与 /stream 并发会互踩）；③ 成功后刷新活动时间戳（空闲巡检的依据）。 */
    if (xSemaphoreTake(s_cam_mutex, portMAX_DELAY) != pdTRUE)
        return -1;

    camera_wake_locked();

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        xSemaphoreGive(s_cam_mutex);
        return -1;
    }

    /* 契约要求：返回的 buf 到下次调用前有效 → 必须拷入自有缓冲后再归还 fb */
    if (fb->len > QSHOU_CAM_BUF_CAP) {
        ESP_LOGW(TAG, "frame too large: %u > %u, dropped",
                 (unsigned)fb->len, (unsigned)QSHOU_CAM_BUF_CAP);
        esp_camera_fb_return(fb);
        xSemaphoreGive(s_cam_mutex);
        return -1;
    }

    memcpy(s_buf, fb->buf, fb->len);
    s_len = fb->len;
    esp_camera_fb_return(fb);
    s_last_capture_us = esp_timer_get_time();

    *buf = s_buf;
    *len = s_len;
    xSemaphoreGive(s_cam_mutex);
    return 0;
}

int platform_camera_set_exposure(int auto_on, int aec_value)
{
    s_exp_auto = auto_on ? 1 : 0;
    if (aec_value < 0)     aec_value = 0;
    if (aec_value > 1200)  aec_value = 1200;
    s_aec_value = aec_value;
    camera_apply_settings_locked_entry();
    return 0;
}

int platform_camera_set_image_adj(int brightness, int contrast, int saturation)
{
    if (brightness < -2) { brightness = -2; }
    if (brightness >  2) { brightness =  2; }
    if (contrast   < -2) { contrast   = -2; }
    if (contrast   >  2) { contrast   =  2; }
    if (saturation < -2) { saturation = -2; }
    if (saturation >  2) { saturation =  2; }
    s_brightness = brightness;
    s_contrast = contrast;
    s_saturation = saturation;
    camera_apply_settings_locked_entry();
    return 0;
}

int platform_camera_set_auto_wb(int on)
{
    s_auto_wb = on ? 1 : 0;
    camera_apply_settings_locked_entry();
    return 0;
}

int platform_camera_set_auto_gain(int on)
{
    s_auto_gain = on ? 1 : 0;
    camera_apply_settings_locked_entry();
    return 0;
}

int platform_camera_set_framesize(int size)
{
    /* 上限 sensor 感知：min(camera_sensor[] 表 max_size, 缓冲实用上限 QSHOU_CAM_MAX_FS)。
     * OV2640 表上限 UXGA(15)，OV5640 表上限 QSXGA(23)。
     * 旧代码写死 13 —— 既挡住 OV2640 的 UXGA，更挡住 OV5640 的 1080p/5MP 档，
     * 换 OV5640 后高分辨率全部不可用（本函数是唯一卡点）。 */
    if (size < 0 || size > camera_max_framesize())
        return -1;
    if (camera_ensure_init() != 0)
        return -1;
    /* ⚠️ 不能只调 s->set_framesize()：组件在 esp_camera_init→cam_config 时按初始
     * frame_size 一次性分配帧缓冲（JPEG 模式 fb_size = w*h/5，VGA≈60KB），运行时
     * 只改传感器寄存器不重分配缓冲 → 切大档 JPEG 撑爆缓冲（cam_hal: FB-OVF →
     * fb_get 4s 超时返回 NULL → /snapshot 503）。2026-10-25 实测 15/16 档复现。
     * 正确做法：esp_camera_reconfigure() = deinit + 按新 frame_size 重分配 fb。
     * 注意：deinit 会 free(s_state) → 旧 sensor_t 指针失效，必须重新获取。
     * 代价：切档瞬间约 0.5–1s 图传中断（显式用户动作可接受；期间 /snapshot
     * 阻塞在锁上排队而非报错）。init 把传感器重置为默认 → 切完重套已存参数。 */
    if (xSemaphoreTake(s_cam_mutex, portMAX_DELAY) != pdTRUE)
        return -1;
    camera_wake_locked();   /* standby 中的 sensor 也走完整重配，先唤醒避免探测边缘 case */
    s_cfg.frame_size = (framesize_t)size;
    esp_err_t err = esp_camera_reconfigure(&s_cfg);
    sensor_t *s = esp_camera_sensor_get();   /* reconfigure 后 s_state 是重新分配的，旧指针已失效 */
    if (err != ESP_OK || !s) {
        xSemaphoreGive(s_cam_mutex);
        ESP_LOGE(TAG, "reconfigure fs=%d failed: %s", size, esp_err_to_name(err));
        return -1;
    }
    s_framesize = (int)s->status.framesize;   /* 读回实际生效值（组件可能夹到 sensor 上限） */
    camera_apply_settings();
    xSemaphoreGive(s_cam_mutex);
    ESP_LOGI(TAG, "frame size requested=%d actual=%d (reconfigured)", size, s_framesize);
    return 0;
}

int platform_camera_get_framesize(int *out)
{
    if (out)
        *out = s_framesize;
    return 0;
}

int platform_camera_get_settings(int *auto_on, int *aec_value, int *brightness,
                                int *contrast, int *saturation,
                                int *auto_wb, int *auto_gain)
{
    if (auto_on)    *auto_on    = s_exp_auto;
    if (aec_value)  *aec_value  = s_aec_value;
    if (brightness) *brightness = s_brightness;
    if (contrast)   *contrast   = s_contrast;
    if (saturation) *saturation = s_saturation;
    if (auto_wb)    *auto_wb    = s_auto_wb;
    if (auto_gain)  *auto_gain  = s_auto_gain;
    return 0;
}

#endif /* PLATFORM_ESP32 */
