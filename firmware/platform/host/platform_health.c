/*
 * platform/host/platform_health.c — host 桩
 * 返回确定性的假遥测，使 health 端点与监控逻辑可被纯逻辑测试覆盖。
 */
#include "platform/platform.h"
#include "platform/platform_sysinfo.h"

#include <string.h>

static uint32_t s_uptime_ms = 0;

uint32_t platform_uptime_ms(void)
{
    s_uptime_ms += 100; /* 每次采样人为推进，便于断言“单调递增” */
    return s_uptime_ms;
}

uint32_t platform_free_heap(void)
{
    return 412344u; /* ~403 KiB 假值 */
}

int platform_wifi_rssi(void)
{
    return -55;
}

const char *platform_reset_reason(void)
{
    return "power-on";
}

/* ----设备信息面板：确定性假值，使面板渲染可被纯逻辑测试覆盖 ---- */

/* 与 partitions.csv 对齐的假分区表：offset/size 刻意用与真机一致的数值，
 * 这样测试能断言「面板把 storage 标成未挂载」这类渲染规则。
 * 字段序：label, offset, size, is_app, is_data, mounted */
static const qshou_part_info s_parts[] = {
    { "factory",  0x10000,  0x400000, 1, 0, 0 },
    { "nvs",      0x9000,   0x6000,   0, 1, 0 },
    { "phy_init", 0xf000,   0x1000,   0, 1, 0 },
    { "storage",  0x410000, 0x100000, 0, 1, 0 },
};

int platform_sysinfo_partitions(qshou_part_info *out, size_t cap)
{
    if (!out || cap == 0) return 0;
    size_t n = sizeof(s_parts) / sizeof(s_parts[0]);
    if (n > cap) n = cap;
    memcpy(out, s_parts, n * sizeof(s_parts[0]));
    return (int)n;
}

void platform_sysinfo_nvs(qshou_nvs_info *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    out->part_size       = 0x6000;          /* 24KB 分区 */
    out->used_entries    = 7;
    out->total_entries   = 180;
    out->free_entries    = 173;
    out->namespace_count = 3;
    out->used_bytes_est  = 7u * 32u;        /* 条目 × 32 字节 */
    out->cap_bytes_est   = 180u * 32u;
    out->valid           = 1;
}

void platform_sysinfo_mem(qshou_mem_info *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    /* 数值取自真机实测（2026-10-02 换用分 region 口径后），保证假值也自洽：
     * heap_* 与 internal_total 同口径（仅内部 RAM），PSRAM 单独报。
     * ⚠️ 别再填 412344 那种"总堆"数字 —— 它会大于 internal_total，
     * 一眼就能看出伪数据，也掩盖了口径错误。 */
    out->heap_free      = 41800u;
    out->heap_min_free  = 38000u;
    out->heap_largest   = 31744u;
    out->internal_total = 333531u;
    out->psram_total    = 8u * 1024u * 1024u;
    out->psram_free     = 7u * 1024u * 1024u;
    out->psram_largest  = 5u * 1024u * 1024u;
}

/* 3 个假任务：覆盖「正常上报」与「查不到任务（valid=0 → 被 routes 跳过）」
 * 两条渲染分支。字段序：name, stack_free（字节）, valid */
static const qshou_task_info s_tasks[] = {
    { "main",       2816, 1 },
    { "httpd",      1024, 1 },
    { "nimble_host",   0, 0 },   /* 故意标 invalid，验证跳过逻辑 */
};

int platform_sysinfo_tasks(qshou_task_info *out, size_t cap)
{
    if (!out || cap == 0) return 0;
    memset(out, 0, cap * sizeof(*out));
    size_t n = sizeof(s_tasks) / sizeof(s_tasks[0]);
    if (n > cap) n = cap;
    memcpy(out, s_tasks, n * sizeof(s_tasks[0]));
    return (int)n;
}
