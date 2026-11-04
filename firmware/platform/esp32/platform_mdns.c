/*
 * platform/esp32/platform_mdns.c — mDNS 服务广播
 *
 * 目的：设备 IP 不固定（切换热点会漂移，实测从 192.168.160.53 漂到
 *       192.168.65.53），让控制端用稳定主机名 qshou-0a0c.local 访问，
 *       免去每次扫网段找 IP。
 *
 * 实现：ESP-IDF 自带 mdns 组件。mdns_init() 在默认事件循环上自动随
 *       netif 起停；注册 _http._tcp(80) 服务后，STA 一拿到 IP 即开始广播。
 *       默认主机名 qshou-<MAC尾两字节> 由本机 WiFi STA MAC 派生
 *       （A 板 0a:0c → qshou-0a0c；B 板 b7:f8 → qshou-b7f8），
 *       两板同网段广播不再重名。
 *
 * 注意：
 *   - mDNS 仅在同一广播域（同 LAN）有效，不过路由器；远程访问仍归
 *     relay/P2P（已按 execution_policy 推迟）。
 *   - 控制端需能解析 .local：macOS/iOS/Linux(avahi) 原生；Windows 需 Bonjour。
 *   - 多设备要防重名：主机名含 MAC 尾段，避免两台同款冲突
 *     （2026-10-16 修复：此前是硬编码 qshou-0a0c，B 板会与 A 板撞名）。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"
#include "platform/platform_mdns.h"
#include "mdns.h"
#include "esp_log.h"
#include "esp_mac.h"       /* esp_read_mac：派生唯一主机名用 */

#define MDNS_TAG        "QSHOU-MDNS"
#define MDNS_HOST       "qshou-0a0c"     /* esp_read_mac 失败时的兜底（= A 板默认名） */
#define MDNS_INSTANCE   "Qshou"      /* 服务实例显示名（允许空格） */
#define MDNS_HTTP_PORT  80              /* 控制/UI 端口（API 与 Web UI 都在 80） */

static bool s_mdns_started = false;

/* 默认主机名：qshou-<WiFi STA MAC 后两字节小写十六进制>。
 * 每块板唯一（MAC 出厂写死），两板同 LAN 广播不撞名；
 * 仍可被 NVS qshou_cfg:mdns_host 覆盖（冷生效）。 */
static void mdns_default_host(char *out, size_t n)
{
    uint8_t mac[6] = {0};
    if (n == 0) return;
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK && n >= 16) {
        snprintf(out, n, "qshou-%02x%02x", mac[4], mac[5]);
    } else {
        snprintf(out, n, "%s", MDNS_HOST);
    }
}

int platform_mdns_start(void)
{
    if (s_mdns_started) return 0;

    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGE(MDNS_TAG, "mdns_init failed: %s (0x%x)", esp_err_to_name(err), err);
        return -1;
    }

    /* 主机名优先级：NVS qshou_cfg:mdns_host（冷生效）> 本机 MAC 派生默认 */
    char host_buf[32] = {0};
    char mdns_cfg[32] = {0};
    const char *host;
    if (platform_cfg_get_str("mdns_host", mdns_cfg, sizeof(mdns_cfg)) && mdns_cfg[0]) {
        host = mdns_cfg;
    } else {
        mdns_default_host(host_buf, sizeof(host_buf));
        host = host_buf;
    }
    err = mdns_hostname_set(host);
    if (err != ESP_OK)
        ESP_LOGE(MDNS_TAG, "mdns_hostname_set failed: %s", esp_err_to_name(err));

    err = mdns_instance_name_set(MDNS_INSTANCE);
    if (err != ESP_OK)
        ESP_LOGE(MDNS_TAG, "mdns_instance_name_set failed: %s", esp_err_to_name(err));

    mdns_txt_item_t txt[] = {
        {"path",  "/"},
        {"board", "esp32-s3"},
        {"app",   "qshou"},
    };
    err = mdns_service_add(MDNS_INSTANCE, "_http", "_tcp", MDNS_HTTP_PORT,
                           txt, sizeof(txt) / sizeof(txt[0]));
    if (err != ESP_OK)
        ESP_LOGE(MDNS_TAG, "mdns_service_add _http failed: %s", esp_err_to_name(err));

    s_mdns_started = true;
    ESP_LOGI(MDNS_TAG, "mDNS up: %s.local (http :%d); instance '%s'",
             host, MDNS_HTTP_PORT, MDNS_INSTANCE);
    return 0;
}

void platform_mdns_stop(void)
{
    if (!s_mdns_started) return;
    mdns_free();
    s_mdns_started = false;
    ESP_LOGI(MDNS_TAG, "mDNS stopped");
}

#endif /* PLATFORM_ESP32 */
