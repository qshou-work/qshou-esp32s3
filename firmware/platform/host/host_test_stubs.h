/*
 * platform/host/host_test_stubs.h — 仅供 host 单元测试使用的 getter 声明
 * 不属于正式固件 API；真实 ESP32 构建不编译此文件。
 */
#ifndef QSHOU_HOST_TEST_STUBS_H
#define QSHOU_HOST_TEST_STUBS_H

#include <stdint.h>

/* 读第 i 次发射的 report（i 从 0 起）。返回累计发射次数；越界时 out 不变。 */
int  host_ble_get_nth(int i, uint8_t *out, int *len);
/* 读第 i 次发射的 Report ID（复合设备：鼠标 1 / 键盘 2 / Consumer 3）。 */
int  host_ble_get_nth_rid(int i);
/* 清空历史，便于用例间隔离。 */
void host_ble_reset(void);

#endif /* QSHOU_HOST_TEST_STUBS_H */
