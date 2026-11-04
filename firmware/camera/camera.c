/*
 * camera/camera.c — 取帧（纯转发到 platform 接缝）
 *
 * 全部硬件细节在 platform_camera_capture；这里只负责缓冲生命周期与错误透传。
 * 真实 MJPEG 编码在 OV2640 + esp32-camera 内完成，host 桩返回合成 JPEG 标记。
 */
#include "camera/camera.h"
#include "platform/platform.h"

int camera_next_jpeg(const uint8_t **buf, size_t *out_len)
{
    return platform_camera_capture(buf, out_len);
}
