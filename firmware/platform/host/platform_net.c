/*
 * platform/host/platform_net.c — host 桩：系统启动接缝
 *
 * 不启动任何真实硬件/网络：只打印日志并返回成功，使 main.c 的
 * 编排逻辑可编译、可运行（零硬件构建验证）。
 * 真实实现见 platform/esp32/platform_net.c（到货后）。
 */
#include "platform/platform.h"
#include "platform/platform_mdns.h"
#include <stdio.h>

int platform_system_init(void)
{
    platform_log("NET", "system_init (host stub)");
    return 0;
}

int platform_ble_init(const char *device_name)
{
    platform_log("NET", "ble_init (host stub) name=%s",
                 device_name ? device_name : "(null)");
    return 0;
}

/* 注意：返回 0 不代表已联网。esp32 侧真实联网由 WiFi 事件驱动，
 * 是否在线要查 platform_wifi_is_connected()。 */
int platform_wifi_init(void)
{
    platform_log("NET", "wifi_init (host stub)");
    return 0;
}

bool platform_wifi_is_connected(void)
{
    return false;   /* host 无真实 WiFi */
}

/* ---- 控制 API：WiFi 实时管理（host 桩；与 esp32 同签名，返回安全默认）---- */
/* ⚠️ platform_wifi_rssi **不在此文件**：规范位置是 platform_health.c
 * （与 esp32 接缝一致）。这里曾重复定义过一次，host 构建到链接阶段才报
 * multiple definition 而失败 —— 且因为是链接期错误，前面 739/1307 步全绿，
 * 极容易误判成"构建被打断"而非代码问题。 */
int  platform_wifi_connect(const char *ssid, const char *pass){ (void)ssid;(void)pass; return 0; }
int  platform_wifi_scan(void){ return 0; }
int  platform_wifi_scan_results(wifi_ap_t *out, int max){ (void)out;(void)max; return 0; }
int  platform_wifi_disconnect(void){ return 0; }
int  platform_wifi_ip(char *buf, size_t n){ if(buf&&n) snprintf(buf,n,"0.0.0.0"); return 0; }

bool platform_ble_is_connected(void)
{
    return false;   /* host 无真实 BLE */
}

int platform_http_start(void)
{
    platform_log("NET", "http_start (host stub)");
    return 0;
}

int platform_stream_start(void)
{
    platform_log("NET", "stream_start (host stub)");
    return 0;
}

int platform_mdns_start(void)
{
    platform_log("NET", "mdns_start (host stub)");
    return 0;
}

void platform_mdns_stop(void)
{
    platform_log("NET", "mdns_stop (host stub)");
}

/* ---- 设备配置 NVS（host 桩：空操作 / 返回未找到）---- */
int platform_cfg_get_str(const char *key, char *out, size_t n){ (void)key; if(out&&n)out[0]=0; return 0; }
int platform_cfg_get_u8 (const char *key, uint8_t *out){ (void)key; if(out)*out=0; return 0; }
int platform_cfg_get_u16(const char *key, uint16_t *out){ (void)key; if(out)*out=0; return 0; }
int platform_cfg_set_str(const char *key, const char *val){ (void)key;(void)val; return 0; }
int platform_cfg_set_u8 (const char *key, uint8_t val){ (void)key;(void)val; return 0; }
int platform_cfg_set_u16(const char *key, uint16_t val){ (void)key;(void)val; return 0; }
int platform_cfg_set_wifi(const char *ssid, const char *pass){ (void)ssid;(void)pass; return 0; }
void platform_request_reboot(void){ platform_log("NET", "reboot requested (host stub)"); }
