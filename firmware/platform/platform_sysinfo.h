/*
 * platform/platform_sysinfo.h — 设备信息面板接缝（存储 / 内存 / 任务栈水位）
 *
 * 独立头文件，**故意不并入 platform.h**：platform.h 被几乎所有 .c 包含，
 * 改它会触发依赖它的全部目标重编（含 IDF 组件，实测全量 ~44min）。
 * 本头只被 platform_health.c（esp32/host 两份）与 main/routes.c 包含。
 *
 * 为什么没有 CPU 负载百分比：
 *   IDF v6.1 已移除 esp_system_get_cpu_usage()，要拿 idle 占比必须开
 *   CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS + CONFIG_FREERTOS_USE_TRACE_FACILITY，
 *   而改 sdkconfig = 全量重编。免重编的替代是「任务栈剩余水位」——
 *   它能直接预警栈溢出，对排查 HID 抖动比粗略的 CPU% 更有用。
 *
 * ⚠️ 本文件所有字段语义都按「实测能拿到什么」定义，不做同名不同义的账：
 *   - nvs_stats_t 里**没有**页（page）口径，只有条目（entry）口径；
 *   - FreeRTOS 未开 trace facility → **拿不到任务栈容量**，只能拿
 *     uxTaskGetStackHighWaterMark()（历史最小剩余）。故本接缝**不报栈容量**，
 *     宁缺勿谎报。
 */

#ifndef QSHOU_PLATFORM_SYSINFO_H
#define QSHOU_PLATFORM_SYSINFO_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 存储：NVS 分区用量（WiFi 凭据 / BLE 绑定 / 标定 profile）----
 * 口径说明：NVS 的账是**条目制**，每个条目固定 32 字节（IDF 官方文档口径）。
 * used_bytes_est / cap_bytes_est 由条目数 × 32 估算，用于面板画进度条；
 * part_size 是分区表里 NVS 分区的真实容量（含尚未纳入条目池的保留页），
 * 两者不相等是正常的 —— 面板同时展示，不做混淆。 */
typedef struct {
    uint32_t part_size;        /* NVS 分区容量（字节，分区表实测） */
    uint32_t used_entries;     /* 已占用条目 */
    uint32_t total_entries;    /* 条目总容量 */
    uint32_t free_entries;     /* 剩余可用于数据的条目 */
    uint32_t namespace_count;  /* 命名空间数量（qshou / nvs.net80211 / bt 等） */
    uint32_t used_bytes_est;   /* 估算占用 = used_entries × 32 */
    uint32_t cap_bytes_est;    /* 条目容量对应上限 = total_entries × 32 */
    int      valid;            /* 0 = 分区不存在或读取失败，下述字段无意义 */
} qshou_nvs_info;

/* ---- 存储：单个分区条目（分区表全景）---- */
typedef struct {
    char     label[24];        /* nvs / phy_init / factory / storage */
    uint32_t offset;
    uint32_t size;
    uint8_t  is_app;           /* 1 = 应用分区 */
    uint8_t  is_data;          /* 1 = 数据分区 */
    uint8_t  mounted;          /* 1 = 已挂载文件系统（本固件恒为 0，见实现注释） */
} qshou_part_info;

/* ---- 内存：内部 RAM + 外置 PSRAM ----
 * 🔴 口径纪律（2026-10-02 真机纠错）：内建 `esp_get_free_heap_size()` 返回的是
 *    **含 PSRAM 在内的全部 region** 空闲量。本板 PSRAM 有 7.6MB 空闲 → 面板上
 *    "内部堆 8.0MB" 与 internal_total 333KB 自相矛盾。故这里一律改用
 *    `heap_caps_*_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)` 分 region 查询，
 *    保证四个 heap_* / internal_total 字段**同口径（仅内部 RAM）**；
 *    PSRAM 单独用 psram_* 报。总可用 = heap_free + psram_free。 */
typedef struct {
    uint32_t heap_free;        /* 当前可用**内部**堆（不含 PSRAM） */
    uint32_t heap_min_free;    /* 内部堆运行以来最小水位（低水位预警） */
    uint32_t heap_largest;     /* 内部堆最大连续可用块（分配失败预判） */
    uint32_t internal_total;   /* 内部 RAM 堆区总量（与上面三项同口径） */
    uint32_t psram_total;      /* PSRAM 总量（板载 8MB，未启用时为 0） */
    uint32_t psram_free;       /* PSRAM 当前可用 */
    uint32_t psram_largest;    /* PSRAM 最大连续块 */
} qshou_mem_info;

/* ---- 任务栈水位（诊断栈溢出，比 CPU% 更实用）----
 * 🔴 本固件未开 configUSE_TRACE_FACILITY，**无法获得任务栈容量**，
 *    故这里只给「历史最小剩余」。数值单位是字节（ESP32 的 StackType_t
 *    = uint8_t，高水位标记本身就是字节口径）。 */
typedef struct {
    char     name[24];
    uint32_t stack_free;       /* 历史最小剩余（字节）—— 接近 0 即将溢出 */
    uint8_t  valid;            /* 0 = 未查到该任务 */
} qshou_task_info;

/*
 * 采集各项信息。单参数为 NULL 即跳过该项。
 * host 桩返回确定性假值，使面板渲染可被纯逻辑测试覆盖。
 */
void platform_sysinfo_nvs(qshou_nvs_info *out);
int  platform_sysinfo_partitions(qshou_part_info *out, size_t cap); /* 返回写入条数 */
void platform_sysinfo_mem(qshou_mem_info *out);
int  platform_sysinfo_tasks(qshou_task_info *out, size_t cap);      /* 返回写入条数 */

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_PLATFORM_SYSINFO_H */
