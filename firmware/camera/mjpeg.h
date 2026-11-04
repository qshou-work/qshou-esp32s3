/*
 * camera/mjpeg.h — MJPEG multipart 边界帧构造（纯逻辑，无硬件依赖）
 *
 * 构造 multipart/x-mixed-replace 协议中“一帧”的内容：
 *   --<boundary>\r\n
 *   Content-Type: image/jpeg\r\n
 *   Content-Length: <jlen>\r\n
 *   \r\n
 *   <jpeg bytes>\r\n
 * 调用方负责在流开头发送协议头，并自行追加结束边界 `--<boundary>--`。
 *
 * 与 controller 侧的 MJPEG 解析（shared/transport.parse_mjpeg_frames）互为
 * 生产/消费两端，边界写法必须一致。
 */
#ifndef QSHOU_MJPEG_H
#define QSHOU_MJPEG_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * mjpeg_part — 把一帧 JPEG 写入 out，返回写入字节数；空间不足返回 0。
 *   boundary : 不含前导 "--" 的边界串（如 "QshouFrame"）
 *   jpeg     : JPEG 数据
 *   jlen     : JPEG 长度
 */
size_t mjpeg_part(uint8_t *out, size_t cap,
                  const char *boundary,
                  const uint8_t *jpeg, size_t jlen);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_MJPEG_H */
