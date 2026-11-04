/*
 * platform/esp32/platform_health.c — ESP32 真实遥测接缝
 * 提供 /api/v1/health 所需的上电时长 / 空闲堆 / WiFi RSSI / 复位原因。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"
#include "platform/platform_sysinfo.h"

#include <string.h>

#include "esp_timer.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_wifi.h"
#include "esp_partition.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

uint32_t platform_uptime_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000LL);
}

uint32_t platform_free_heap(void)
{
    return (uint32_t)esp_get_free_heap_size();
}

int platform_wifi_rssi(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
        return ap.rssi;
    return 0;   /* 未连接 */
}

const char *platform_reset_reason(void)
{
    switch (esp_reset_reason()) {
        case ESP_RST_POWERON:  return "power-on";
        case ESP_RST_SW:       return "software";
        case ESP_RST_PANIC:    return "panic";
        case ESP_RST_INT_WDT:  return "int-wdt";
        case ESP_RST_TASK_WDT: return "task-wdt";
        case ESP_RST_WDT:      return "wdt";
        case ESP_RST_BROWNOUT: return "brownout";
        case ESP_RST_DEEPSLEEP:return "deep-sleep";
        case ESP_RST_EXT:      return "external";
        default:               return "unknown";
    }
}

/* ==================================================================
 * 设备信息面板（/api/v1/sysinfo）
 *
 * 存储 = NVS 分区用量 + 分区表全景。
 * 🔴 注意：分区表里那 1MB storage（spiffs）**从未被挂载** —— 固件里没有
 *    任何 esp_vfs_spiffs_init/littlefs_init 调用，故文件系统占用报不出来，
 *    面板会显式标 storage 为「未挂载」而不是谎报 0。
 *
 * ⚠️ API 事实（已核对 IDF v6.1 头文件，勿凭记忆改）：
 *   - nvs_stats_t = {used_entries, free_entries, available_entries,
 *                    total_entries, namespace_count}，**无 page 字段**；
 *   - 分区迭代必须用 esp_partition_find()（返回 iterator）；
 *     esp_partition_find_first() 返回的是 const esp_partition_t*，
 *     拿它当迭代器喂 esp_partition_next() 会直接编译失败；
 *   - 本固件 CONFIG_FREERTOS_USE_TRACE_FACILITY 未开 → vTaskGetInfo()
 *     不可用，且没有任何公开 API 能拿到「任务栈容量」，
 *     只能用 uxTaskGetStackHighWaterMark()（历史最小剩余）。
 * ================================================================== */

/* NVS 分区名（与 partitions.csv 一致；若改名这里也要改） */
#define QSHOU_NVS_PARTITION "nvs"
/* NVS 单条目固定 32 字节（IDF 官方文档：每 32 字节新数据消耗一个条目） */
#define QSHOU_NVS_ENTRY_BYTES 32u

void platform_sysinfo_nvs(qshou_nvs_info *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));

    nvs_stats_t st;
    if (nvs_get_stats(QSHOU_NVS_PARTITION, &st) != ESP_OK) {
        out->valid = 0;   /* 分区不存在或读取失败 */
        return;
    }
    const esp_partition_t *p = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_NVS,
        QSHOU_NVS_PARTITION);
    if (!p) {
        out->valid = 0;
        return;
    }
    out->part_size       = (uint32_t)p->size;
    out->used_entries    = (uint32_t)st.used_entries;
    out->total_entries   = (uint32_t)st.total_entries;
    out->free_entries    = (uint32_t)st.available_entries;
    out->namespace_count = (uint32_t)st.namespace_count;
    out->used_bytes_est  = out->used_entries  * QSHOU_NVS_ENTRY_BYTES;
    out->cap_bytes_est   = out->total_entries * QSHOU_NVS_ENTRY_BYTES;
    out->valid           = 1;
}

/* 遍历 app + data 两类分区。返回写入 out 的条数。 */
int platform_sysinfo_partitions(qshou_part_info *out, size_t cap)
{
    if (!out || cap == 0) return 0;
    int n = 0;
    for (int t = 0; t < 2; t++) {
        esp_partition_type_t type = t ? ESP_PARTITION_TYPE_DATA
                                      : ESP_PARTITION_TYPE_APP;
        /* 🔴 迭代必须用 esp_partition_find；find_first 返回的是分区指针。 */
        esp_partition_iterator_t it =
            esp_partition_find(type, ESP_PARTITION_SUBTYPE_ANY, NULL);
        while (it && (size_t)n < cap) {
            const esp_partition_t *p = esp_partition_get(it);
            if (p) {
                qshou_part_info *d = &out[n];
                memset(d, 0, sizeof(*d));
                strncpy(d->label, p->label, sizeof(d->label) - 1);
                d->offset = (uint32_t)p->address;
                d->size   = (uint32_t)p->size;
                d->is_app  = (p->type == ESP_PARTITION_TYPE_APP)  ? 1 : 0;
                d->is_data = (p->type == ESP_PARTITION_TYPE_DATA) ? 1 : 0;
                /* 本固件没有任何 esp_vfs_spiffs/littlefs 挂载调用，
                 * 故 storage 分区 mounted 恒为 0 —— 这是事实，不是缺省。 */
                d->mounted = 0;
                n++;
            }
            it = esp_partition_next(it);
        }
        if (it) esp_partition_iterator_release(it);
    }
    return n;
}

void platform_sysinfo_mem(qshou_mem_info *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    /* 🔴 必须分 region 查：esp_get_free_heap_size() / esp_get_minimum_free_heap_size()
     * 统计的是**含 PSRAM 的全部 region**，直接填进「内部堆」字段会自相矛盾
     * （真机实测：heap_free 报 8.0MB，而 internal_total 只有 333KB —— 因为
     *  PSRAM 空闲 7.6MB 被算进来了）。故四个内部字段统一用本 caps，
     *  PSRAM 走单独的 psram_* 字段，两者相加才是总可用。 */
    const uint32_t INT8 = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
    out->heap_free      = (uint32_t)heap_caps_get_free_size(INT8);
    out->heap_min_free  = (uint32_t)heap_caps_get_minimum_free_size(INT8);
    out->heap_largest   = (uint32_t)heap_caps_get_largest_free_block(INT8);
    out->internal_total = (uint32_t)heap_caps_get_total_size(INT8);
    /* PSRAM：板载 8MB Octal；若板子没配 PSRAM，caps 查询返回 0（不是错误） */
    out->psram_total    = (uint32_t)heap_caps_get_total_size(MALLOC_CAP_SPIRAM);
    out->psram_free     = (uint32_t)heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    out->psram_largest  = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM);
}

/* 需要关注的任务。名字必须与创建时**逐字一致**（且 ≤16 字节，
 * CONFIG_FREERTOS_MAX_TASK_NAME_LEN=16），否则查不到 → 静默少报一项。
 * 取值依据（均已在本仓库/IDF 中核对）：
 *   "main"         IDF 启动流程创建 app_main 所在任务，名字就叫 main
 *   "nimble_host"  components/bt/.../nimble_port_freertos.c: xTaskCreate(..., "nimble_host", ...)
 *   "httpd"        esp_http_server: httpd_os_thread_create(&..., "httpd", ...)
 *   "mjpeg_stream" platform_net.c: xTaskCreate(mjpeg_stream_task, "mjpeg_stream", ...)
 *   "gesture"      platform_ble.c: xTaskCreate(gesture_task, "gesture", 4096, ...)
 *   "qshou_led"     platform_led.c: xTaskCreate(led_task, "qshou_led", ...)
 *   "qshou_wscan"   platform_net.c: xTaskCreate(wifi_boot_task, "qshou_wscan", 4096, ...)
 * 名字对不上就标 valid=0 跳过 —— 绝不硬编码栈大小猜测，宁可少报也不谎报。 */
static const char *s_watch_tasks[] = {
    "main", "nimble_host", "httpd", "mjpeg_stream", "gesture", "qshou_led", "qshou_wscan",
};

int platform_sysinfo_tasks(qshou_task_info *out, size_t cap)
{
    if (!out || cap == 0) return 0;
    memset(out, 0, cap * sizeof(*out));
    size_t n = 0;
    for (size_t i = 0; i < sizeof(s_watch_tasks) / sizeof(s_watch_tasks[0]); i++) {
        if (n >= cap) break;
        TaskHandle_t h = xTaskGetHandle(s_watch_tasks[i]);
        if (!h) continue;                    /* 任务不存在：不占位，静默跳过 */
        /* 单位换算：ESP32 的 StackType_t = uint8_t，高水位标记本身即字节口径，
         * 乘 sizeof(StackType_t) 只为在其他架构上仍成立（^1 无副作用）。 */
        UBaseType_t hw = uxTaskGetStackHighWaterMark(h);
        qshou_task_info *d = &out[n];
        memset(d, 0, sizeof(*d));
        /* 名字用我们自己传入的字符串：未开 trace facility 时
         * INCLUDE_pcTaskGetTaskName 为 0，pcTaskGetName() 不可用。 */
        strncpy(d->name, s_watch_tasks[i], sizeof(d->name) - 1);
        d->stack_free = (uint32_t)hw * (uint32_t)sizeof(StackType_t);
        d->valid = 1;
        n++;
    }
    return (int)n;
}

#endif /* PLATFORM_ESP32 */
