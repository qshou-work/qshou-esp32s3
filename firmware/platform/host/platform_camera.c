/*
 * platform/host/platform_camera.c — host 桩
 * 返回一张带 JPEG SOI/EOI 标记的合成小缓冲，足以驱动 MJPEG 边界帧构造
 * 与 http 推流路径的纯逻辑测试。真实 OV2640 取帧由 platform/esp32 实现。
 */
#include "platform/platform.h"
#include "platform/platform_camera.h"

/* 最小“假 JPEG”：SOI(FFD8) ... EOI(FFD9)，中间填零。 */
static const uint8_t SAMPLE_JPEG[] = {
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xD9
};

int platform_camera_capture(const uint8_t **buf, size_t *len)
{
    if (!buf || !len)
        return -1;
    *buf = SAMPLE_JPEG;
    *len = sizeof(SAMPLE_JPEG);
    return 0;
}

/* host 桩：相机参数调节为无操作（零硬件阶段没有真实传感器）。 */
int platform_camera_set_exposure(int auto_on, int aec_value)
{
    (void)auto_on; (void)aec_value;
    return 0;
}
int platform_camera_set_image_adj(int brightness, int contrast, int saturation)
{
    (void)brightness; (void)contrast; (void)saturation;
    return 0;
}
int platform_camera_set_auto_wb(int on)
{
    (void)on;
    return 0;
}
int platform_camera_set_auto_gain(int on)
{
    (void)on;
    return 0;
}
int platform_camera_get_settings(int *auto_on, int *aec_value, int *brightness,
                                int *contrast, int *saturation,
                                int *auto_wb, int *auto_gain)
{
    if (auto_on)    *auto_on    = 1;
    if (aec_value)  *aec_value  = 0;
    if (brightness) *brightness = 0;
    if (contrast)   *contrast   = 0;
    if (saturation) *saturation = 0;
    if (auto_wb)    *auto_wb    = 1;
    if (auto_gain)  *auto_gain  = 1;
    return 0;
}

/* host 桩：分辨率切换为无操作（零硬件阶段没有真实传感器）。 */
int platform_camera_set_framesize(int size)
{
    (void)size;
    return 0;
}
int platform_camera_get_framesize(int *out)
{
    if (out) *out = 10;   /* 桩默认 VGA=10，与设备端编译默认值一致（本构建枚举 10=640×480） */
    return 0;
}
