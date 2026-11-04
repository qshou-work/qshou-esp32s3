/*
 * platform/platform_mdns.h — mDNS 广播接缝声明（独立头，避免污染 platform.h
 * 触发全量重编译）。仅被 main.c 与 platform_mdns.c / host 桩包含。
 */
#ifndef QSHOU_PLATFORM_MDNS_H
#define QSHOU_PLATFORM_MDNS_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 启动 mDNS 广播（esp32: espressif/mdns 组件，主机名默认按本机 WiFi STA MAC
 * 尾段派生 qshou-<尾两字节>.local，如 A 板 qshou-0a0c / B 板 qshou-b7f8，
 * 可被 NVS qshou_cfg:mdns_host 覆盖；host: 空操作）。失败不致命，控制端仍可走
 * IP 直连。仅在同一广播域（同 LAN）有效，不过路由器；远程访问仍归 relay/P2P（已推迟）。 */
int  platform_mdns_start(void);
void platform_mdns_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_PLATFORM_MDNS_H */
