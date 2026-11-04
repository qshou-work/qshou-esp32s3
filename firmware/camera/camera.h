/*
 * camera/camera.h — 摄像头取帧接口（依赖 platform 接缝）
 */
#ifndef QSHOU_CAMERA_H
#define QSHOU_CAMERA_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * camera_next_jpeg — 取下一帧 JPEG。
 *   buf/out_len : 指向内部缓冲（下次调用前需拷贝）。
 * 返回 0 成功，-1 失败。真实取帧走 platform_camera_capture（seam_video_source）。
 */
int camera_next_jpeg(const uint8_t **buf, size_t *out_len);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_CAMERA_H */
