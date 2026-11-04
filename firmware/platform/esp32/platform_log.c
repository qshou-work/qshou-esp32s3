/*
 * platform/esp32/platform_log.c — ESP32 日志接缝（重定向到 ESP_LOGx）
 * 保持与 host 桩相同的 printf 风格签名，便于纯逻辑代码无差别调用。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"

#include <stdio.h>
#include <stdarg.h>

#include "esp_log.h"

void platform_log(const char *tag, const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    ESP_LOGI(tag ? tag : "QSHOU", "%s", buf);
}

#endif /* PLATFORM_ESP32 */
