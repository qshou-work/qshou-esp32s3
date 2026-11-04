/*
 * platform/host/platform_log.c — host 桩（stdout）
 * esp32 实现将重定向到 ESP_LOGx。
 */
#include "platform/platform.h"
#include <stdio.h>
#include <stdarg.h>

void platform_log(const char *tag, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    if (tag) fprintf(stdout, "[%s] ", tag);
    vfprintf(stdout, fmt, ap);
    fprintf(stdout, "\n");
    va_end(ap);
}
