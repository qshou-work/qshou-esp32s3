/*
 * camera/mjpeg.c — MJPEG 单帧边界构造（纯逻辑）
 */
#include "camera/mjpeg.h"
#include <string.h>
#include <stdio.h>

size_t mjpeg_part(uint8_t *out, size_t cap,
                  const char *boundary,
                  const uint8_t *jpeg, size_t jlen)
{
    if (!out || cap == 0 || !boundary || !jpeg)
        return 0;

    /* 头部（不含结尾空字节）：--<b>\r\nContent-Type: image/jpeg\r\nContent-Length: <n>\r\n\r\n */
    const char *fmt = "--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n";
    int hlen = snprintf((char *)out, cap, fmt, boundary, jlen);
    if (hlen < 0 || (size_t)hlen >= cap)
        return 0;                       /* 头部放不下 */

    size_t off = (size_t)hlen;
    if (off + jlen + 2 > cap)
        return 0;                       /* 加上 JPEG 与结尾 CRLF 放不下 */

    memcpy(out + off, jpeg, jlen);
    off += jlen;
    out[off++] = '\r';
    out[off++] = '\n';
    return off;
}
