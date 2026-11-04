/*
 * platform/esp32/platform_net.c — ESP32 真实启动接缝
 *
 * 实现 platform.h 的系统启动 API：
 *   platform_system_init  → esp_netif / event loop / NVS
 *   platform_ble_init     → NimBLE（ESP-IDF 自带，非 Arduino BLEDevice）
 *   platform_wifi_init    → WiFi STA（凭据从 NVS 读，不硬编码）
 *   platform_http_start   → esp_http_server(80)：/api/v1/ 端点 + / (UI)
 *                           + /stream（仅 302 跳到 81）+ /snapshot（单帧）
 *   platform_stream_start → 相机预热 + 拉起**独立** MJPEG 服务器任务（端口 81）
 *
 * ⚠️ 图传必须独立于 httpd：esp_http_server 只有一个 httpd 任务，持续推流的
 *    while(1) handler 会把它占死，使所有输入请求在推流期间得不到处理。
 *
 * 说明：BLE HID 描述符与发射由 platform/esp32/platform_ble.c 负责，
 * 本文件只做“启动编排”。HTTP 端点转发到纯逻辑 routes_handle，
 * 因此业务逻辑仍可 host 单测。
 *
 * 编译：仅在 PLATFORM_ESP32 时启用（见 main/CMakeLists.txt 的 esp32 分支）。
 */
#ifdef PLATFORM_ESP32

#include "platform/platform.h"
#include "main/routes.h"

#include <string.h>
#include <strings.h>   /* strcasecmp：探测 Upgrade 头（大小写不敏感） */
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <errno.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"    /* esp_restart：配置冷生效 */
#include "esp_err.h"       /* esp_err_to_name：事件通道的 WS 收帧失败日志 */
#include "esp_coexist.h"   /* esp_coex_preference_set：2.4G 共存仲裁 */

/* 由 platform/esp32/platform_ble.c 提供：最近一次 HID 发射时刻（ms）。
 * 推流循环据此判断「HID 活动窗口」，窗口内暂停发帧把时隙让给 BLE。 */
extern uint32_t platform_ble_last_tx_ms(void);

#define TAG              "QSHOU"
#define WIFI_SSID_KEY    "wifi_ssid"
#define WIFI_PASS_KEY    "wifi_pass"
/* 512 装不下 /api/v1/sysinfo（NVS + 内存 + 分区表 + 任务栈水位）。
 * 取值依据（两条上界取大者 + 余量，勿拍脑袋）：
 *   A) /sysinfo 最坏 1762 字节：routes.c 的 SYSINFO_MAX_PARTS=8 / SYSINFO_MAX_TASKS=8，
 *      每个数字按 uint32 最宽 10 位十进制、label/name 各按 23 字节上限代入。
 *   B) /schema    最坏 2378 字节（本轮 P5 实测：23 个端点的 path/methods/desc/params）。
 * 🔴 P5 修正：原为 2048，只按 (A) 的 1762 留 14% 余量，**漏算了 (B)** ——
 *    而 B 更大 → handle_schema 的 snprintf 返回「本应写入长度」= 2378 ≥ 2048
 *    → 真机 `GET /api/v1/schema` 恒返回 **500**。这是 P0 以来一直存在的真 bug：
 *    Agent 唯一的自描述入口不可用，症状却表现为「Agent 拉 schema 失败」。
 *    教训：加端点不只影响 ROUTES[]/max_uri_handlers，**也影响本缓冲**。
 *    守卫：tests/test_control_contract.py::test_schema_response_fits_resp_cap
 *    与 test_schema.py::TestSysinfoContract::test_response_cap_fits_worst_case。
 * 取 4096 给两条都留 ~40% 余量（缓冲是 .bss，不占 httpd 任务栈；8MB PSRAM 在板）。
 * 注：若 NVS 配了更小的 http_resp_cap，运行时会被钳到 [1024,8192]，
 *     但**下限是本值** —— 低于它等于把上面的两个端点弄成恒 500，故不再下探。 */
#define RESP_CAP         4096

/* 由 main.c 提供的推流帧构造（复用纯逻辑 camera/mjpeg） */
extern size_t firmware_build_frame(uint8_t *out, size_t cap);

/* 从 NVS 读一个字符串（不存在返回 0） */
static int nvs_get_str_safe(const char *key, char *out, size_t n)
{
    nvs_handle_t h;
    if (nvs_open("qshou", NVS_READONLY, &h) != ESP_OK) return 0;
    size_t len = n;
    esp_err_t err = nvs_get_str(h, key, out, &len);
    nvs_close(h);
    return (err == ESP_OK && len > 0) ? 1 : 0;
}

/* ---- 设备配置 NVS（独立命名空间 qshou_cfg，与 WiFi 凭据 qshou / BLE 绑定 ns 隔离）----
 * 这些项均为「冷生效」：写入后由 routes.c 触发 esp_restart，boot 时各接缝读取应用。
 * 钳制边界写在写入端（routes.c）与读取端两处，防止越界值让服务起不来。 */
#define CFG_NS "qshou_cfg"

/* 冷生效重启请求标志：handle_settings 写入 NVS 后置位，http_bridge 在把 200
 * 响应发出后再延时重启，保证浏览器先收到「设备重启中」反馈而非连接被重置。 */
static bool s_need_reboot = false;

/* 关键约定：键缺失时**必须将 *out 清零**，否则调用方若复用同一变量
 * （如 GET 预填时 b/w 跨多次读取复用），后一个缺失键会继承前一个
 * 存在键的值，导致回显串位（曾出现 http_max_uri 误显为 cam_quality）。
 * 所有调用方都以返回值为准做存在性判断，故清零不影响既有逻辑。 */
static int nvs_cfg_get_str(const char *key, char *out, size_t n)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READONLY, &h) != ESP_OK) {
        if (out && n) out[0] = '\0';
        return 0;
    }
    size_t len = n;
    esp_err_t err = nvs_get_str(h, key, out, &len);
    nvs_close(h);
    if (err != ESP_OK) { if (out && n) out[0] = '\0'; return 0; }
    return (len > 0) ? 1 : 0;
}
static int nvs_cfg_get_u8(const char *key, uint8_t *out)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READONLY, &h) != ESP_OK) { *out = 0; return 0; }
    esp_err_t err = nvs_get_u8(h, key, out);
    nvs_close(h);
    if (err != ESP_OK) { *out = 0; return 0; }
    return 1;
}
static int nvs_cfg_get_u16(const char *key, uint16_t *out)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READONLY, &h) != ESP_OK) { *out = 0; return 0; }
    esp_err_t err = nvs_get_u16(h, key, out);
    nvs_close(h);
    if (err != ESP_OK) { *out = 0; return 0; }
    return 1;
}
static int nvs_cfg_set_str(const char *key, const char *val)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_str(h, key, val);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return (err == ESP_OK) ? 0 : -1;
}
static int nvs_cfg_set_u8(const char *key, uint8_t val)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_u8(h, key, val);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return (err == ESP_OK) ? 0 : -1;
}
static int nvs_cfg_set_u16(const char *key, uint16_t val)
{
    nvs_handle_t h;
    if (nvs_open(CFG_NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_u16(h, key, val);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return (err == ESP_OK) ? 0 : -1;
}

int platform_cfg_get_str(const char *key, char *out, size_t n){ return nvs_cfg_get_str(key, out, n); }
int platform_cfg_get_u8 (const char *key, uint8_t *out){ return nvs_cfg_get_u8(key, out); }
int platform_cfg_get_u16(const char *key, uint16_t *out){ return nvs_cfg_get_u16(key, out); }
int platform_cfg_set_str(const char *key, const char *val){ return nvs_cfg_set_str(key, val); }
int platform_cfg_set_u8 (const char *key, uint8_t val){ return nvs_cfg_set_u8(key, val); }
int platform_cfg_set_u16(const char *key, uint16_t val){ return nvs_cfg_set_u16(key, val); }

/* WiFi 凭据写入 ns qshou（与 BLE 绑定 ns 分开，写 WiFi 绝不触碰 bonding）。
 * pass 为空表示不改密码。 */
int platform_cfg_set_wifi(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    if (nvs_open("qshou", NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_str(h, WIFI_SSID_KEY, ssid);
    if (err == ESP_OK && pass && pass[0]) err = nvs_set_str(h, WIFI_PASS_KEY, pass);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return (err == ESP_OK) ? 0 : -1;
}

void platform_request_reboot(void){ s_need_reboot = true; }

/* ---- 控制 API：WiFi 实时管理 —— 真实实现见文件后部 platform_wifi_init 之后 ---- */

/* ---- HTTP 请求 → routes_handle 的桥接 ---- */
static esp_err_t http_bridge(httpd_req_t *req)
{
    char body[512] = {0};
    int to_read = (int)(req->content_len < sizeof(body) - 1
                        ? req->content_len : sizeof(body) - 1);
    if (to_read > 0) {
        int got = httpd_req_recv(req, body, to_read);
        /* 接收超时非致命（请求体已读完），其余负值为真实错误 */
        if (got < 0 && got != HTTPD_SOCK_ERR_TIMEOUT) return ESP_FAIL;
        body[got > 0 ? got : 0] = '\0';
    }

    /* 响应缓冲：编译期 RESP_CAP 为**下限**（不是默认值）——/sysinfo 与 /schema 的
     * 最坏响应都超过 2048，配小了会让这两个端点恒返回 500（详见 RESP_CAP 处注释）。
     * NVS(qshou_cfg:http_resp_cap) 只能配**更大**；静态兜底 s_resp_floor 保证即使
     * malloc 失败仍有编译期容量可用。 */
    static char s_resp_floor[RESP_CAP];
    static char *s_resp = NULL;
    static size_t s_resp_cap = 0;
    if (s_resp == NULL) {
        uint16_t cap = RESP_CAP;
        if (nvs_cfg_get_u16("http_resp_cap", &cap)) {
            if (cap < RESP_CAP) cap = RESP_CAP;      /* 不允许配到装不下最坏响应 */
            else if (cap > 8192) cap = 8192;
        }
        s_resp = malloc(cap);
        if (s_resp) s_resp_cap = cap;
        else { s_resp = s_resp_floor; s_resp_cap = RESP_CAP; }
    }
    char auth_buf[64] = {0};
    httpd_req_get_hdr_value_str(req, "Authorization", auth_buf, sizeof(auth_buf));
    const char *auth = auth_buf[0] ? auth_buf : NULL;

    http_method_t m = (req->method == HTTP_GET) ? M_GET : M_POST;
    int status = routes_handle(m, req->uri, body, auth, s_resp, s_resp_cap);

    const char *st = (status == 200) ? "200 OK"
                   : (status == 400) ? "400 Bad Request"
                   : (status == 403) ? "403 Forbidden"
                   : (status == 404) ? "404 Not Found"
                   : (status == 405) ? "405 Method Not Allowed"
                   : (status == 501) ? "501 Not Implemented"
                                     : "500 Internal Server Error";
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_status(req, st);
    esp_err_t send_err = httpd_resp_send(req, s_resp, HTTPD_RESP_USE_STRLEN);
    /* 冷生效：配置已写入 NVS，先让 200（含 "rebooting"）发出，再重启。
     * 若在此前的 routes 处理里请求了重启，300ms 后硬复位，boot 时读取新配置。 */
    if (s_need_reboot) {
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
    }
    return send_err;
}

/* ---- 设备端 Web UI：GET / 返回内嵌的 ui.html（由 EMBED_TXTFILES 提供）---- */
/* ESP-IDF 的 EMBED_TXTFILES 生成 objcopy 风格符号 _binary_<name>_start/end */
extern const uint8_t _binary_ui_html_start[];
extern const uint8_t _binary_ui_html_end[];

static esp_err_t http_ui_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, (const char *)_binary_ui_html_start,
                    (ssize_t)(_binary_ui_html_end - _binary_ui_html_start));
    return ESP_OK;
}

/* ---- MJPEG 图传：独立 socket 服务器（端口 81）+ 独立任务 --------------
 *
 * 一帧 multipart 内容 = 头部(--boundary / Content-Type / Content-Length) + JPEG + CRLF，
 * 由纯逻辑 mjpeg_part() 组装（与控制器侧解析同源，见 camera/mjpeg.c）。
 * VGA JPEG 单帧可达 20–60KB，组装缓冲必须放 PSRAM（内部 SRAM 放不下 128KB）。
 *
 * 浏览器侧：<img src="http://<ip>:81/stream"> 原生渲染 multipart/x-mixed-replace，
 * 无需 JS 解码。单帧 <img src="/snapshot"> 走 image/jpeg（仍在 httpd 的 80 端口）。
 *
 * ★★ 为什么 MJPEG 必须脱离 esp_http_server —— 这是「推流期间鼠标键盘不响应、
 *    动作要等推流结束才一起冲刷」的**真正根因**（2026-10-01 读 IDF 源码坐实）：
 *    esp_http_server 的 httpd_thread() 是**单任务**（components/esp_http_server/
 *    src/httpd_main.c）：select() → httpd_sess_process() → 调用 handler。
 *    /stream 的 handler 是 while(1) 死循环，一旦进入就**永久独占唯一那个 httpd
 *    任务**。此后浏览器发来的 /api/v1/gesture（鼠标移动）、/api/v1/key（键盘/文本）
 *    虽然 TCP 连接已建立，但 httpd 再也不会回到 select()，请求一直挂着不被读取
 *    → 表现为「推流期间输入完全不动，停流后集中冲刷」。
 *    这也解释了此前调画质 / 帧率 / 节流窗口 / WiFi PS **全部无效**的原因：
 *    它们都不在这个层面。（旧注释曾误称「esp_http_server 为每个连接分配独立
 *    server 任务」——错，全实例只有一个 "httpd" 任务，勿再据此设计。）
 *
 *    现在 MJPEG 由**独立 lwIP socket 服务器 + 独立任务**承载，与 httpd 彻底
 *    解耦：图传再忙也只阻塞它自己，API 与 HID 永远可响应。任务优先级(4)低于
 *    httpd(5) 与 gesture(5)，确保输入侧能立即抢占 CPU。
 *
 *    选「独立 socket」而非「再开一个 httpd 实例」的原因：本项目
 *    CONFIG_LWIP_MAX_SOCKETS=10，而每个 httpd 实例内部就要占 3 个 socket
 *    （listen + ctrl UDP + msg UDP），两个实例会逼近上限；独立 socket 服务器
 *    只需 2 个（listen + client），且无需改 sdkconfig（避免全量重编译）。
 *
 * 推流与 BLE 的 2.4G 射频争用是物理事实，但**代码层不再对 BLE/WiFi 做流控**——
 * 交给 ESP32 自带的 WiFi↔BLE 共存仲裁器 + lwIP/TCP 窗口 + 相机取帧速率去调度。
 * 决策（2026-10-03）：用户希望观察设备自身的流控能力；且原先「HID 窗口内暂停推流」
 * 会把防休眠净零抖动也当成活动，导致开启防休眠时画面冻结。详见对话记录。 */
/* 单帧 multipart 组装上限 = 帧头(~100B) + 单帧 JPEG + 结尾 CRLF。
 * 2026-10-25：随 OV5640 高分辨率支持从 128KB 扩到 512KB+512 ——
 * VGA JPEG(q16) 仅 15-35KB，但 1080p 可达 200-350KB，旧容量下 mjpeg_part
 * 直接返回 0（静默丢帧，推流黑屏）。PSRAM 8MB 足够容纳。 */
#define STREAM_PART_CAP  (512 * 1024 + 512)

/* 推流节拍：仅作「通用 pacing」防止忙等/重复帧洪泛，不感知 BLE/WiFi 状态 ——
 * 射频争用（推流 vs BLE HID）完全交给 ESP32 自带的 WiFi↔BLE 共存仲裁器 + TCP 窗口调度。
 * 50ms ≈ 上限 ~20fps；这是固定节拍而非流控，可按需调（调小更流畅但更占无线）。 */
#define STREAM_FRAME_DELAY_MS   50

/* 已移除：原先的 HID 活动窗口节流（HID_THROTTLE_WINDOW_MS / STREAM_THROTTLED_DELAY_MS）。
 * 代码不再因 BLE 发射而暂停推流；射频争用改由硬件共存仲裁器 + TCP 窗口调度。
 * 保留宏定义（置 0）仅为避免任何遗留引用断编。 */
#define HID_THROTTLE_WINDOW_MS  0
#define STREAM_THROTTLED_DELAY_MS 0

/* 独立 MJPEG 服务器参数（与 httpd 的 80 端口分开，互不牵连） */
#define MJPEG_STREAM_PORT        81      /* 图传专用端口；80 保留给 API / UI */
#define MJPEG_STREAM_BACKLOG     1       /* 同一时刻只服务一个预览客户端 */
#define MJPEG_STREAM_STACK       5120
#define MJPEG_STREAM_PRIO        4       /* 低于 httpd(5) 与 gesture(5)：图传不抢输入 */
#define MJPEG_RCV_TIMEOUT_S      5
#define MJPEG_SND_TIMEOUT_S      3

static uint8_t *s_part_buf = NULL;

/* ---- 80 端口上的 /stream：302 跳转到 81 端口 ----
 * 真正的图传已搬到独立服务器（见 mjpeg_stream_task）。此处保留跳转，
 * 使旧链接 / 书签仍然可用；Host 头缺失时退化为一段说明文本。 */
static esp_err_t http_stream_redirect_handler(httpd_req_t *req)
{
    char host[64] = {0};
    if (httpd_req_get_hdr_value_str(req, "Host", host, sizeof host) == ESP_OK && host[0]) {
        char *colon = strchr(host, ':');
        if (colon) *colon = '\0';                 /* 去掉原端口，换用 81 */
        char loc[96];
        snprintf(loc, sizeof loc, "http://%s:%d/stream", host, MJPEG_STREAM_PORT);
        httpd_resp_set_status(req, "302 Found");
        httpd_resp_set_hdr(req, "Location", loc);
        return httpd_resp_send(req, NULL, 0);
    }
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_send(req, "MJPEG stream moved to port 81",
                           HTTPD_RESP_USE_STRLEN);
}

/* 全量发送：lwIP 的 send() 可能只发一部分，必须循环；超时/出错按对端断开处理 */
static int mjpeg_send_all(int fd, const uint8_t *buf, size_t len)
{
    size_t off = 0;
    while (off < len) {
        int n = send(fd, buf + off, len - off, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;                    /* EAGAIN = 发送超时（对端不收） */
        }
        if (n == 0) return -1;
        off += (size_t)n;
    }
    return 0;
}

/* 读到 "\r\n\r\n"（请求头结束）为止；只做最小解析，不校验方法与路径 */
static int mjpeg_read_request(int fd)
{
    uint8_t ch = 0;
    int lf = 0;
    for (int i = 0; i < 2048; i++) {
        int n = recv(fd, &ch, 1, 0);
        if (n <= 0) return -1;
        if (ch == '\n') {
            if (++lf >= 2) return 0;
        } else if (ch != '\r') {
            lf = 0;
        }
    }
    return -1;
}

/* ---- MJPEG 图传主任务（独立于 httpd，永不阻塞 API / HID） ---- */
static void mjpeg_stream_task(void *arg)
{
    (void)arg;

    /* 组帧缓冲：PSRAM 优先，内部 RAM 兜底 */
    if (!s_part_buf) {
        s_part_buf = (uint8_t *)heap_caps_malloc(STREAM_PART_CAP, MALLOC_CAP_SPIRAM);
        if (!s_part_buf)
            s_part_buf = (uint8_t *)malloc(STREAM_PART_CAP);
    }
    if (!s_part_buf) {
        ESP_LOGE(TAG, "mjpeg: frame buffer alloc failed, stream disabled");
        vTaskDelete(NULL);
        return;
    }

    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    if (lfd < 0) {
        ESP_LOGE(TAG, "mjpeg: socket() failed errno=%d", errno);
        vTaskDelete(NULL);
        return;
    }
    struct timeval rcv_tv = { .tv_sec = MJPEG_RCV_TIMEOUT_S, .tv_usec = 0 };
    setsockopt(lfd, SOL_SOCKET, SO_RCVTIMEO, &rcv_tv, sizeof rcv_tv);

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(MJPEG_STREAM_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(lfd, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        listen(lfd, MJPEG_STREAM_BACKLOG) < 0) {
        ESP_LOGE(TAG, "mjpeg: bind/listen :%d failed errno=%d", MJPEG_STREAM_PORT, errno);
        close(lfd);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "mjpeg stream server up on :%d (own task, prio %d)",
             MJPEG_STREAM_PORT, MJPEG_STREAM_PRIO);

    while (1) {
        struct sockaddr_in cli;
        socklen_t cli_len = sizeof cli;
        int fd = accept(lfd, (struct sockaddr *)&cli, &cli_len);
        if (fd < 0) continue;                       /* 超时或无连接，继续等待 */

        /* 收发都必须有超时：否则对端静默断开时任务会永久卡在 send() */
        struct timeval snd_tv = { .tv_sec = MJPEG_SND_TIMEOUT_S, .tv_usec = 0 };
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &snd_tv, sizeof snd_tv);
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &snd_tv, sizeof snd_tv);

        if (mjpeg_read_request(fd) != 0) {
            close(fd);
            continue;
        }

        /* 边界串必须与 mjpeg_part() 使用的一致，否则浏览器解析不出帧 */
        const char *hdr =
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: multipart/x-mixed-replace;boundary=" QSHOU_STREAM_BOUNDARY "\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n";
        if (mjpeg_send_all(fd, (const uint8_t *)hdr, strlen(hdr)) != 0) {
            close(fd);
            continue;
        }
        ESP_LOGI(TAG, "mjpeg client connected");
        platform_led_set_streaming(true);   /* 状态灯切青色（推流中） */

        while (1) {
            /* 不再因 BLE 发射而暂停推流：射频争用交由硬件共存仲裁器 + TCP 窗口调度，
             * 仅保留最小通用 pacing（见 STREAM_FRAME_DELAY_MS）。 */
            size_t n = firmware_build_frame(s_part_buf, STREAM_PART_CAP);
            if (n == 0) {
                /* 取帧失败（相机未就绪 / 暂无新帧）：短暂退避，避免空转烧 CPU */
                vTaskDelay(pdMS_TO_TICKS(100));
                continue;
            }
            if (mjpeg_send_all(fd, s_part_buf, n) != 0) {
                ESP_LOGI(TAG, "mjpeg client disconnected");
                break;
            }
            /* 最小通用 pacing：仅避免忙等，不感知 BLE/WiFi（射频争用交设备自带共存调度） */
            vTaskDelay(pdMS_TO_TICKS(STREAM_FRAME_DELAY_MS));
        }
        platform_led_set_streaming(false);  /* 客户端断开，状态灯回绿/蓝 */
        close(fd);
    }
}

static esp_err_t http_snapshot_handler(httpd_req_t *req)
{
    const uint8_t *buf = NULL;
    size_t len = 0;
    /* 单帧直发（image/jpeg）。注：与 /stream 共用相机内部缓冲，
     * 两者同时使用时可能取到撕裂帧 —— 单客户端场景可接受。 */
    if (platform_camera_capture(&buf, &len) != 0 || !buf || len == 0) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        return httpd_resp_send(req, "camera unavailable", HTTPD_RESP_USE_STRLEN);
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, (const char *)buf, (ssize_t)len);
}

static const char *ROUTES[] = {
    "/api/v1/health",
    "/api/v1/led",
    "/api/v1/tap",
    "/api/v1/gesture",
    "/api/v1/swipe",
    "/api/v1/key",
    "/api/v1/calibrate/point",
    "/api/v1/calibrate/commit",
    "/api/v1/camera",        /* 真实传感器参数：曝光/亮度/对比度/饱和度 */
    "/api/v1/sysinfo",       /* 设备信息面板：存储/内存/任务栈余量 */
    "/api/v1/settings",      /* 设备配置：GET 预填 + POST 写 NVS 后重启（冷生效） */
    "/api/v1/schema",        /* 自描述全部端点（Agent 发现用） */
    "/api/v1/wifi/status",
    "/api/v1/wifi/scan",
    "/api/v1/wifi/connect",
    "/api/v1/wifi/disconnect",
    "/api/v1/ble/status",
    "/api/v1/ble/advertise",
    "/api/v1/ble/disconnect",
    "/api/v1/ble/bonds",
    "/api/v1/hid",             /* HID 路由状态(GET)/策略(POST) —— 与下面三条子路径并列注册，
                                * ⚠️ 少了这一条，httpd 层直接 404 "Nothing matches the given
                                * URI"，routes_handle 里写得再对也永远到不了 */
    "/api/v1/hid/mouse",
    "/api/v1/hid/keyboard",
    "/api/v1/hid/consumer",
    "/api/v1/gpio",          /* GPIO 列表 */
    "/api/v1/gpio/*",        /* 单引脚 /api/v1/gpio/{pin}（ESP-IDF httpd 通配前缀） */
    /* 注意：/api/v1/events **不在**此表 —— WebSocket 端点必须用
     * HTTP_GET + is_websocket 单独注册（见下方 events_ws_handler），
     * 走通用 http_bridge（HTTP_ANY）会导致握手永远不被处理。 */
};

/* ---- 上报 platform_ble_emit 的发射号（BLE 侧实现持有）---- */
/* 由 platform/esp32/platform_ble.c 中定义；此处仅作弱符号占位避免链接错。 */
__attribute__((weak)) int platform_esp32_ble_ready(void) { return 0; }

int platform_system_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) return -1;

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_LOGI(TAG, "system init done");
    return 0;
}

int platform_ble_init(const char *device_name)
{
    /* 真实 BLE HID 由 platform_ble.c 的 platform_esp32_ble_start 完成；
     * 本函数只转达设备名。到货后补齐 NimBLE Absolute Mouse 描述符注册。 */
    extern int platform_esp32_ble_start(const char *name);
    return platform_esp32_ble_start(device_name);
}

/* ---- WiFi 状态与事件 ---- */
#define WIFI_MAX_RETRY  10          /* 超出次数后停止重试并明确报错 */

static int s_wifi_retry    = 0;     /* 已重试次数（拿到 IP 后清零） */
static bool s_wifi_connected = false;
static bool s_pending_switch     = false;  /* 用户请求切 AP：断开后由 DISCONNECTED 事件套新凭据重连 */
static bool s_stay_disconnected  = false;  /* 用户主动断开：断开后保持离线、不自动重连 */
static esp_ip4_addr_t s_ip       = {0};    /* 当前 STA IP（GOT_IP 时刷新） */
static int  s_ap_num            = 0;       /* 最近一次扫描的可见 AP 数 */

/* 断开原因 → 明文。带原始数值打印，未覆盖的原因也不会丢信息。 */
static const char *wifi_reason_text(wifi_err_reason_t r)
{
    switch (r) {
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:            return "4way handshake timeout";
        case WIFI_REASON_GROUP_KEY_UPDATE_TIMEOUT:          return "group key update timeout";
        case WIFI_REASON_MIC_FAILURE:                       return "MIC failure";
        case WIFI_REASON_802_1X_AUTH_FAILED:                return "802.1x auth failed";
        case WIFI_REASON_CIPHER_SUITE_REJECTED:             return "cipher suite rejected";
        case WIFI_REASON_INVALID_PMKID:                     return "invalid PMKID";
        case WIFI_REASON_BEACON_TIMEOUT:                    return "beacon timeout";
        case WIFI_REASON_NO_AP_FOUND:                       return "AP not found";
        case WIFI_REASON_AUTH_FAIL:                         return "auth failed (check password)";
        case WIFI_REASON_ASSOC_FAIL:                        return "assoc failed";
        case WIFI_REASON_HANDSHAKE_TIMEOUT:                 return "handshake timeout";
        case WIFI_REASON_CONNECTION_FAIL:                   return "connection failed";
        case WIFI_REASON_AP_TSF_RESET:                      return "AP TSF reset";
        case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY: return "AP found but security incompatible";
        case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD: return "AP below authmode threshold";
        case WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD:     return "AP below RSSI threshold";
        default:                                            return "see reason code above";
    }
}

/* ---- 启动扫描诊断：把“看不见 AP”变成可读事实 ---- */
#define SCAN_MAX_AP  20

static wifi_ap_record_t s_aps[SCAN_MAX_AP];
static char s_target_ssid[64];          /* 当前候选的 SSID（仅用于日志，由 apply_cred 刷新） */

/* 候选网络表：NVS 中的 wifi_ssid/wifi_pass 为主，下列为固件内置备用网络。
 * 设备上电扫描后连「可见且已配置」的第一条；某条放弃后轮到下一条。
 * 内置备用的凭据编译进 app 分区，不写 NVS，故不会破坏 BLE bonding。 */
typedef struct {
    char ssid[64];
    char pass[64];
} wifi_cred_t;

static wifi_cred_t s_creds[8];      /* 运行期候选列表（NVS 主 + 内置备用） */
static int        s_ncreds    = 0;  /* 实际候选数 */
static int        s_cred_idx  = 0;  /* 当前在用候选（断线放弃后轮转） */
static int        s_chosen_idx = -1; /* 扫描后选中的首个可见候选 */

/* 固件内置备用网络（按优先级从高到低追加；凭据写在 app 分区，不进 NVS）。
 *
 * ⚠️ 真实凭据**不入库**：放在同目录的 qshou_wifi_creds.h（.gitignore 已排除），
 *    由 QSHOU_EXTRA_WIFI_CREDS 宏提供。该文件缺失时由 main/CMakeLists.txt
 *    在配置期从 .example 生成空表 —— 所以这里的 #include 是**无条件**的：
 *    可选包含（__has_include）的依赖不进 gcc -MD 依赖表，ccache 直连模式
 *    会静默复用「空表版本」的旧 .obj（2026-10-27 实测，排查很久）。 */
#include "qshou_wifi_creds.h"

#ifndef QSHOU_EXTRA_WIFI_CREDS
#  define QSHOU_EXTRA_WIFI_CREDS   /* 兜底：宏缺失时同样按空表处理 */
#endif

static const wifi_cred_t s_extra_creds[] = {
    QSHOU_EXTRA_WIFI_CREDS
    { "", "" },   /* 哨兵：C 不允许空数组初始化；N_EXTRA 已减 1，永不被使用 */
};
#define N_EXTRA  ((int)(sizeof(s_extra_creds) / sizeof(s_extra_creds[0])) - 1)

/* 把第 idx 个候选写入 WiFi STA 配置，并刷新 s_target_ssid（仅用于日志）。
 * 返回 esp_wifi_set_config 的结果：切换凭据失败不能静默吞掉，否则会拿着
 * 上一条候选的凭据去连，日志却显示新 SSID，极难排查。
 * 注：IDF 允许 start 之后调用（用于运行期换 AP）；但连接进行中会返回
 * ESP_ERR_WIFI_STATE，故须在断开态调用。 */
static esp_err_t apply_cred(int idx)
{
    if (idx < 0 || idx >= s_ncreds) return ESP_ERR_INVALID_ARG;
    const wifi_cred_t *c = &s_creds[idx];
    strncpy(s_target_ssid, c->ssid, sizeof(s_target_ssid) - 1);
    s_target_ssid[sizeof(s_target_ssid) - 1] = '\0';

    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid,     c->ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, c->pass, sizeof(wc.sta.password) - 1);
    /* 不设 threshold.authmode 门槛：写死 WPA2_PSK 会静默拒绝 WPA/WPA2 混合或
     * WPA3-only 的 AP（reason 211）。AP 实际 authmode 连接成功后由
     * health/rssi 侧观测，此处保持最大兼容。 */
    /* ── listen_interval：STA 收 beacon 的间隔（单位 = AP beacon 间隔）──
     * ⚠️ 不写就是 0，而 IDF 明确规定 defaults to 3 if set to 0
     *   (esp_wifi_types_generic.h)，即隐式每 3 个 beacon 才醒；此处显式设 1。
     * ⚠️ 仅在 WIFI_PS_MAX_MODEM 下生效；本配置 MIN_MODEM 下由 AP 的 DTIM
     *   决定，本字段实际被忽略。保留是为显性化该反直觉默认值（2026-10-02
     *   已实测 MAX_MODEM+li=1 无收益，PS 已回退 MIN_MODEM，见 set_ps 处）。 */
    wc.sta.listen_interval = 1;
    return esp_wifi_set_config(WIFI_IF_STA, &wc);
}

/*
 * reason=201 (NO_AP_FOUND) 时，仅靠猜是分不清 5GHz-only / 隐藏 SSID / 信号弱的。
 * 这里做一次主动扫描，把附近所有 AP 的 SSID / RSSI / 信道 / authmode 全打印出来，
 * 一次性收敛结论。show_hidden=1 才能捞到隐藏 SSID（其 ssid 字段为空串）。
 */
static void wifi_scan_report(void)
{
    wifi_scan_config_t sc  = {0};
    sc.ssid       = NULL;
    sc.bssid      = NULL;
    sc.channel    = 0;                  /* 0 = 全信道 */
    sc.scan_type  = WIFI_SCAN_TYPE_ACTIVE;
    sc.show_hidden = true;

    ESP_LOGI(TAG, "scanning 2.4GHz (show_hidden=1)...");
    esp_err_t err = esp_wifi_scan_start(&sc, true);   /* block=true */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "scan start failed: %s (0x%x)", esp_err_to_name(err), err);
        return;
    }

    uint16_t num = 0;
    esp_err_t g = esp_wifi_scan_get_ap_num(&num);
    if (g != ESP_OK) {
        ESP_LOGE(TAG, "get_ap_num failed: %s (0x%x)", esp_err_to_name(g), g);
        return;
    }
    if (num == 0) {
        ESP_LOGW(TAG, "scan: 0 AP visible → 查天线是否接好 / 是否身处 2.4GHz 覆盖");
        return;
    }
    ESP_LOGI(TAG, "scan: %u AP visible, listing up to %d", num, SCAN_MAX_AP);
    if (num > SCAN_MAX_AP) num = SCAN_MAX_AP;

    g = esp_wifi_scan_get_ap_records(&num, s_aps);
    if (g != ESP_OK) {
        ESP_LOGE(TAG, "get_ap_records failed: %s (0x%x)", esp_err_to_name(g), g);
        return;
    }

    bool any = false;
    for (int i = 0; i < (int)num; i++) {
        ESP_LOGI(TAG, "  [%02d] ssid=%-32s rssi=%3d ch=%2d auth=%d",
                 i, (const char *)s_aps[i].ssid, s_aps[i].rssi,
                 (int)s_aps[i].primary, (int)s_aps[i].authmode);
    }
    s_chosen_idx = -1;
    for (int c = 0; c < s_ncreds; c++) {
        bool found = false;
        for (int i = 0; i < (int)num; i++) {
            if (strcmp((const char *)s_aps[i].ssid, s_creds[c].ssid) == 0) {
                found = true;
                break;
            }
        }
        ESP_LOGI(TAG, "candidate[%d] '%s' %s", c, s_creds[c].ssid,
                 found ? "IS visible on 2.4GHz" : "NOT visible");
        if (found) {
            any = true;
            if (s_chosen_idx < 0) s_chosen_idx = c;
        }
    }
    if (!any) {
        ESP_LOGW(TAG, "none of the configured networks visible —— 5GHz-only / 隐藏SSID / 信号不足");
    }
}

/* 扫描后首次连接。放在独立 task 里：扫描是阻塞的，不能堵在事件回调上下文中。
 * 之后的断线重试走 wifi_event_cb 直接 esp_wifi_connect()，不再重扫。 */
static void wifi_boot_task(void *arg)
{
    (void)arg;
    wifi_scan_report();
    /* 扫描选出「首个可见候选」；都没可见则仍尝试候选 0（可能是隐藏 SSID）。 */
    if (s_chosen_idx >= 0) {
        s_cred_idx = s_chosen_idx;
    }
    esp_err_t ac = apply_cred(s_cred_idx);
    if (ac != ESP_OK) {
        ESP_LOGE(TAG, "apply_cred(%d '%s') failed: %s",
                 s_cred_idx, s_creds[s_cred_idx].ssid, esp_err_to_name(ac));
    } else {
        ESP_LOGI(TAG, "scan done, now connecting to candidate[%d/%d] '%s'",
                 s_cred_idx, s_ncreds, s_target_ssid);
    }
    esp_wifi_connect();
    vTaskDelete(NULL);
}

/* WiFi 事件：STA_START 触发首次连接；DISCONNECTED 打印原因并重试 */
static void wifi_event_cb(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    static bool s_boot_task_started = false;

    if (id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "wifi STA started");
        if (!s_boot_task_started) {
            s_boot_task_started = true;
            xTaskCreate(wifi_boot_task, "qshou_wscan", 4096, NULL, 5, NULL);
        } else {
            esp_wifi_connect();
        }
    } else if (id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *evc = (wifi_event_sta_connected_t *)data;
        ESP_LOGI(TAG, "wifi associated: authmode=%d ch=%d, waiting for IP",
                 (int)evc->authmode, (int)evc->channel);
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *ev = (wifi_event_sta_disconnected_t *)data;
        s_wifi_connected = false;
        if (s_pending_switch) {
            s_pending_switch = false;
            s_wifi_retry = 0;
            ESP_LOGI(TAG, "switching to requested AP '%s'", s_creds[s_cred_idx].ssid);
            esp_err_t ac = apply_cred(s_cred_idx);
            if (ac == ESP_OK) esp_wifi_connect();
            else ESP_LOGE(TAG, "switch AP apply_cred failed: %s", esp_err_to_name(ac));
            return;
        }
        if (s_stay_disconnected) {
            ESP_LOGI(TAG, "user-requested disconnect; staying offline (auto-reconnect disabled)");
            return;
        }
        ESP_LOGW(TAG, "wifi disconnected: reason=%d (%s)",
                 (int)ev->reason, wifi_reason_text(ev->reason));
        if (s_wifi_retry < WIFI_MAX_RETRY) {
            s_wifi_retry++;
            ESP_LOGI(TAG, "retry %d/%d", s_wifi_retry, WIFI_MAX_RETRY);
            esp_wifi_connect();
        } else {
            /* 当前候选重试耗尽 → 轮转到下一个候选（循环）。
             * s_ncreds==1 时 next==s_cred_idx，即退化为原来的彻底放弃。 */
            s_wifi_retry = 0;
            int next = (s_cred_idx + 1) % s_ncreds;
            if (next == s_cred_idx) {
                ESP_LOGE(TAG, "give up after %d retries on all %d candidate(s);"
                              " check SSID/password/authmode",
                         WIFI_MAX_RETRY, s_ncreds);
            } else {
                s_cred_idx = next;
                ESP_LOGW(TAG, "candidate gave up, switching to candidate[%d/%d] '%s'",
                         s_cred_idx, s_ncreds, s_creds[s_cred_idx].ssid);
                esp_err_t ac = apply_cred(s_cred_idx);
                if (ac != ESP_OK) {
                    ESP_LOGE(TAG, "apply_cred('%s') failed: %s —— 保持旧配置重连",
                             s_creds[s_cred_idx].ssid, esp_err_to_name(ac));
                }
                esp_wifi_connect();
            }
        }
    }
}

static void ip_event_cb(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    if (id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        s_wifi_connected = true;
        s_wifi_retry = 0;
        s_ip = ev->ip_info.ip;
        s_pending_switch = false;
        s_stay_disconnected = false;
        /* 注意：IDF v6.1 的 ip_event_got_ip_t 不含 rssi 字段，
         * RSSI 由 platform_wifi_rssi()（esp_wifi_sta_get_ap_info）单独取。 */
        ESP_LOGI(TAG, "got ip:" IPSTR, IP2STR(&s_ip));
    }
}

bool platform_wifi_is_connected(void)
{
    return s_wifi_connected;
}

int platform_wifi_init(void)
{
    char ssid[64] = {0}, pass[64] = {0};

    /* 构建候选网络列表：NVS 主网络优先，其后是固件内置备用网络。
     * 两者都保留 —— 扫描后连首个可见的；主网络不可见时自动退到备用。 */
    s_ncreds   = 0;
    s_cred_idx = 0;
    if (nvs_get_str_safe(WIFI_SSID_KEY, ssid, sizeof(ssid))) {
        strncpy(s_creds[s_ncreds].ssid, ssid, sizeof(s_creds[0].ssid) - 1);
        nvs_get_str_safe(WIFI_PASS_KEY, pass, sizeof(pass));
        strncpy(s_creds[s_ncreds].pass, pass, sizeof(s_creds[0].pass) - 1);
        s_ncreds++;
    } else {
        ESP_LOGW(TAG, "no wifi_ssid in NVS; will rely on built-in fallback network(s)");
    }
    for (int i = 0; i < (int)N_EXTRA; i++) {
        if (s_ncreds >= (int)(sizeof(s_creds) / sizeof(s_creds[0]))) break;
        strncpy(s_creds[s_ncreds].ssid, s_extra_creds[i].ssid,
                sizeof(s_creds[0].ssid) - 1);
        strncpy(s_creds[s_ncreds].pass, s_extra_creds[i].pass,
                sizeof(s_creds[0].pass) - 1);
        s_ncreds++;
    }
    if (s_ncreds == 0) {
        ESP_LOGW(TAG, "no wifi credential at all; skipping STA (configure then reboot)");
        return -1;   /* 非致命 */
    }

    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* 事件回调必须在 esp_wifi_start() 之前注册，否则 STA_START 会漏掉，
     * 结果就是“驱动起来了但从不发起连接”（曾真实发生过的静默故障）。 */
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_cb, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &ip_event_cb, NULL));

    /* 写入首个候选（真正 set_config 在 apply_cred 内），随后启动 STA；
     * 之后由 wifi_boot_task 依扫描结果切换到首个可见候选。 */

    /* 真正的 wifi_config_t 组装（ssid/pass/listen_interval/authmode 门槛）
     * 已收敛到 apply_cred()，切换候选时复用同一处，不会漏改。
     * ⚠️ 调用顺序铁律：esp_wifi_set_config() 要求「目标接口已启用」——
     *    esp_wifi.h 原文 "can be called only when specified interface is
     *    enabled"，官方 station 示例也是 set_mode → set_config → start。
     *    曾把 apply_cred 放在 set_mode 之前：在旧 dirty IDF 上侥幸可用，
     *    纯净 IDF v6.1 直接返回 ESP_ERR_WIFI_MODE(0x3005)，被 ESP_ERROR_CHECK
     *    捕获 → abort → 重启死循环（B 板实测，2026-10-16，串口回溯命中本行）。 */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(apply_cred(s_cred_idx));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* ── 2.4G 共存仲裁：退回官方默认 BALANCE ──
     * ESP32-S3 的 WiFi 与 BLE 共用一套射频与共存交换器，运行期默认偏好就是
     * ESP_COEX_PREFER_BALANCE（WiFi/BT 各取所需、动态平衡）。
     * 曾试过 ESP_COEX_PREFER_BT（偏向 BLE）+ 关 WiFi 省电(WIFI_PS_NONE)，
     * 结果 HID 输入整体失效——aggressive 的 BT 偏向 + PS 关闭让 WiFi 持续
     * 抢占 RF、BLE 反而拿不到稳定时隙。故撤掉该偏向，交回默认 PS。
     * HID 延迟的改善改由「推流降帧(B)+HID 活动窗口自动节流(D)」承担，
     * 二者实测有效且完全不触碰射频仲裁，不会再生回归。 */
    esp_err_t coex = esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
    if (coex != ESP_OK)
        ESP_LOGW(TAG, "esp_coex_preference_set(BALANCE) failed: %s", esp_err_to_name(coex));

    /* ── 实测结论（2026-10-02 方案A）：MAX_MODEM + listen_interval=1 **无效**，已回退 ──
     *
     * 试验：设 ps=MAX_MODEM + listen_interval=1（每 beacon 醒，替代默认的 DTIM/3×beacon）。
     * 串口确认生效无误（"Set ps type: 2"，无 fallback）。
     * 结果（同参数 `tools/diag_ping_correlate.py 120` 前后对比）：
     *       改动前 MIN_MODEM : 设备 p50=368ms avg=464ms p90=1035ms 丢 11/80
     *       改动后 MAX_MODEM : 设备 p50=557ms avg=703ms p90=1492ms 丢 10/78
     *   → **延迟不降反升约 51%**，无收益，故回退。
     *
     * ⚠️ 该对比含混淆变量：测量期间 `wifi_rssi` 由 -23dBm 漂移到 -53~-82dBm，
     *    链路质量本身在恶化，故上述数值**不宜过度解读**；但“没有任何改善”这一点
     *    是明确的，且与更早的**推流对照**结论一致——推流让 STA 常驻活跃(等效不睡)
     *    时 p50 仍 405ms ≈ 空闲 368ms，说明**瓶颈不在省电/唤醒节奏**。
     *
     * 🔴 由此得到一条决策规则：**别再靠调 PS / listen_interval 压 ping。**
     *    真正的主因是 RSSI 在时间尺度上大幅漂移（历史实测 -19→-23→-82→-39 dBm，
     *    摆幅可达 16~27dB，静态环境本应 ±3dB 内）→ 优先排查**天线接触/遮挡/2.4G 干扰**，
     *    其次才是应用层（见 docs/latency_diagnosis_2026-10-02.md）。 */
    esp_err_t ps = esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    if (ps != ESP_OK)
        ESP_LOGW(TAG, "esp_wifi_set_ps(MIN_MODEM) failed: %s", esp_err_to_name(ps));

    ESP_LOGI(TAG, "wifi STA init done, %d candidate(s), primary='%s'"
                 " (coex=balance/default, ps=min_modem)",
             s_ncreds, s_creds[0].ssid);
    return 0;
}

/* ---- 控制 API：WiFi 实时管理（P2 真实现，复用上面事件回调与候选表）---- */

/* authmode → 我们 wifi_ap_t.auth 的整型编码（platform.h 注释约定）。 */
static int wifi_auth_to_code(wifi_auth_mode_t m)
{
    switch (m) {
        case WIFI_AUTH_OPEN:          return 0;
        case WIFI_AUTH_WEP:           return 1;
        case WIFI_AUTH_WPA_PSK:       return 2;
        case WIFI_AUTH_WPA2_PSK:      return 3;
        case WIFI_AUTH_WPA_WPA2_PSK:  return 4;
        case WIFI_AUTH_WPA3_PSK:      return 5;
        case WIFI_AUTH_WPA2_WPA3_PSK: return 6;
        case WIFI_AUTH_WAPI_PSK:      return 7;
        default:                      return (int)m;
    }
}

int platform_wifi_connect(const char *ssid, const char *pass)
{
    if (!ssid || ssid[0] == '\0') return -1;                 /* SSID 必填 */
    if (s_cred_idx < 0 || s_cred_idx >= s_ncreds) return -1; /* WiFi 未初始化 */

    /* 把请求的凭据写进当前候选槽，复用 apply_cred + 事件回调的重连逻辑 */
    strncpy(s_creds[s_cred_idx].ssid, ssid, sizeof(s_creds[0].ssid) - 1);
    s_creds[s_cred_idx].ssid[sizeof(s_creds[0].ssid) - 1] = '\0';
    if (pass && pass[0]) {
        strncpy(s_creds[s_cred_idx].pass, pass, sizeof(s_creds[0].pass) - 1);
        s_creds[s_cred_idx].pass[sizeof(s_creds[0].pass) - 1] = '\0';
    } else {
        s_creds[s_cred_idx].pass[0] = '\0';
    }
    s_wifi_retry = 0;
    s_stay_disconnected = false;

    if (!s_wifi_connected) {
        /* 离线：直接套配置并发起连接 */
        esp_err_t ac = apply_cred(s_cred_idx);
        if (ac != ESP_OK) { ESP_LOGE(TAG, "connect apply_cred failed: %s", esp_err_to_name(ac)); return -2; }
        esp_err_t c = esp_wifi_connect();
        if (c != ESP_OK) { ESP_LOGE(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(c)); return -3; }
        return 0;
    }
    /* 在线：先断开，由 DISCONNECTED 事件回调套新凭据重连（避开 ESP_ERR_WIFI_STATE）*/
    s_pending_switch = true;
    esp_wifi_disconnect();
    return 0;
}

int platform_wifi_scan(void)
{
    wifi_scan_config_t sc = {0};
    sc.ssid        = NULL;
    sc.bssid       = NULL;
    sc.channel     = 0;            /* 0 = 全信道 */
    sc.scan_type   = WIFI_SCAN_TYPE_ACTIVE;
    sc.show_hidden = true;
    esp_err_t err = esp_wifi_scan_start(&sc, true);   /* block=true */
    if (err != ESP_OK) { ESP_LOGE(TAG, "scan start failed: %s", esp_err_to_name(err)); return -1; }

    uint16_t num = 0;
    if (esp_wifi_scan_get_ap_num(&num) != ESP_OK) return -1;
    if (num > SCAN_MAX_AP) num = SCAN_MAX_AP;
    if (esp_wifi_scan_get_ap_records(&num, s_aps) != ESP_OK) return -1;
    s_ap_num = (int)num;
    ESP_LOGI(TAG, "scan done: %d AP visible", s_ap_num);
    return 0;
}

int platform_wifi_scan_results(wifi_ap_t *out, int max)
{
    if (!out || max <= 0) return 0;
    int n = s_ap_num < max ? s_ap_num : max;
    for (int i = 0; i < n; i++) {
        wifi_ap_record_t *r = &s_aps[i];
        strncpy(out[i].ssid, (const char *)r->ssid, sizeof(out[i].ssid) - 1);
        out[i].ssid[sizeof(out[i].ssid) - 1] = '\0';
        out[i].rssi = r->rssi;
        snprintf(out[i].bssid, sizeof(out[i].bssid),
                 "%02X:%02X:%02X:%02X:%02X:%02X",
                 r->bssid[0], r->bssid[1], r->bssid[2],
                 r->bssid[3], r->bssid[4], r->bssid[5]);
        out[i].auth = wifi_auth_to_code(r->authmode);
    }
    return n;
}

int platform_wifi_disconnect(void)
{
    s_stay_disconnected = true;   /* 断开后保持离线，不自动重连 */
    esp_wifi_disconnect();
    return 0;
}

int platform_wifi_ip(char *buf, size_t n)
{
    if (!buf || n == 0) return -1;
    if (s_ip.addr == 0) { snprintf(buf, n, "0.0.0.0"); return 0; }
    snprintf(buf, n, IPSTR, IP2STR(&s_ip));
    return 0;
}

/* ==================== 事件通道：WebSocket /api/v1/events ====================
 *
 * 双向：客户端可发 {"action":"ping"/"get"/...}，设备可主动推事件。
 *
 * 为何用 httpd 原生 WS（CONFIG_HTTPD_WS_SUPPORT）而非独立 socket：
 *   - 省掉手写 RFC6455 握手（IDF v6.1 已把 SHA1 挪进 PSA 私有头，
 *     mbedtls_sha1_* 不可直接用）+ 帧解析，代码量与出错面大减。
 *   - 不会霸占 httpd 唯一任务：握手成功后 httpd 把该 session 标记
 *     for_async_req，此后不再进入 select 集合，但**仍会在收到数据时
 *     回调 ws_handler**（httpd_parse.c: sd->ws_handshake_done 分支）。
 *     这与 MJPEG 用 while(1) 占死任务是两种机制，别混淆。
 *
 * 推送方向用 httpd_ws_send_frame_async()：它内部直接调 sess->send_fn
 * 把 payload 同步拷贝进 socket 缓冲，故调用者可以传栈上缓冲。
 * 注意 CONFIG_HTTPD_QUEUE_WORK_BLOCKING 故意未开 → httpd_queue_work
 * 在队列满时返回 ESP_FAIL，我们据此丢弃该事件，绝不阻塞调用方任务
 *（相机/BLE/HTTP 都不能被事件推送拖住）。
 */
#define EVENTS_SUB_MAX 4   /* 订阅者上限：max_open_sockets=7 已占 3 个内部 socket */

static httpd_handle_t s_ev_srv   = NULL;   /* 供异步发送用的 server 句柄 */
static int  s_ev_fd[EVENTS_SUB_MAX] = { -1, -1, -1, -1 };
static volatile int s_ev_sub_count = 0;     /* httpd 任务内被增删 */

static int ev_sub_add(int fd)
{
    for (int i = 0; i < EVENTS_SUB_MAX; i++) {
        if (s_ev_fd[i] < 0) {
            s_ev_fd[i] = fd;
            s_ev_sub_count++;
            return 0;
        }
    }
    return -1;   /* 已满 */
}

static void ev_sub_del(int fd)
{
    for (int i = 0; i < EVENTS_SUB_MAX; i++) {
        if (s_ev_fd[i] == fd) {
            s_ev_fd[i] = -1;
            s_ev_sub_count--;
            return;
        }
    }
}

static int ev_sub_find(int fd)
{
    for (int i = 0; i < EVENTS_SUB_MAX; i++) {
        if (s_ev_fd[i] == fd) return i;
    }
    return -1;
}

/* 惰性回收僵尸订阅条目。
 *
 * 🔴 为什么需要：`ev_sub_del()` 只在收到 CLOSE 帧时被调用（WS 帧路径），
 * 而客户端被 kill / 网络中断 / 拔网线时根本走不到那里 —— fd 就永久留在
 * 表里。更糟的是 lwIP 会**复用 fd**：后来的普通 GET 可能恰好命中那个僵尸
 * fd 的数值，于是「这个 fd 在不在订阅表里」不再等价于「这是不是一次 WS
 * 数据帧」，P4 时这里就误判过（表现为 GET /api/v1/events 恒返回 0 字节）。
 *
 * 现在判定不再依赖订阅表（见 events_ws_handler），僵尸条目最多让
 * subscribers 计数偏高；本函数在广播前与读计数时顺手清掉，让计数回到真值。
 * httpd_ws_get_fd_info() 内部走 httpd_sess_get()，在 httpd 任务内调用会
 * 命中「当前请求 session」快捷路径，不遍历会话表。
 */
static void ev_sub_reap(void)
{
    if (!s_ev_srv) return;
    for (int i = 0; i < EVENTS_SUB_MAX; i++) {
        const int fd = s_ev_fd[i];
        if (fd < 0) continue;
        /* ⚠️ 这行与 handler 里的判据**语义相同**，故刻意写成 `!=(...)` 的
         * 取反形式：若两边文本一字不差，反向验证脚本按字面串替换时会命中
         * 第一个（这里），handler 的判据根本没被改动 → 变异无效、守卫生效性
         * 无从证明（踩过一次）。 */
        if (HTTPD_WS_CLIENT_WEBSOCKET !=
            httpd_ws_get_fd_info(s_ev_srv, fd)) {
            s_ev_fd[i] = -1;
            s_ev_sub_count--;
            ESP_LOGI(TAG, "events: reap stale subscriber fd %d", fd);
        }
    }
}

/* 说明：设备侧**没有**「握手即主动推帧」这条路径。
 * httpd 在 WS 握手完成后只在收到客户端帧时才回调 handler
 * （esp_http_server/src/httpd_parse.c: sd->ws_handshake_done 分支 → 先
 * httpd_ws_get_frame_type() 读帧，再 sd->ws_handler(r)）。
 * 因此确认订阅必须绑在「客户端第一帧」上；设备主动推送走
 * platform_events_broadcast()（任意任务可调，非阻塞）。 */
static esp_err_t events_ws_handler(httpd_req_t *req)
{
    const int fd = httpd_req_to_sockfd(req);

/* 🔴 无论下面走哪个分支，都要先回收僵尸订阅 —— 否则 subscribers 会永远虚高。
 * 必须在**判普通 GET 之前**：普通 GET 分支只回状态 JSON、不做别的，若把 reap
 * 放在 WS 分支里，读到的计数就永远是上一次 WS 会话留下的旧值
 * （真机实测：正常 close 帧后连查 3 次普通 GET，subscribers 恒为 1）。
 * 此时我们已在 httpd 任务内（本函数是 handler），httpd_sess_get 走
 * 「当前请求 session」快捷路径，不遍历会话表。*/
    ev_sub_reap();

/* 🔴 该 URI 以 is_websocket=true 注册，但**两类完全不同的请求都会进这里**：
 *   (a) 普通 GET（无 Upgrade 头）—— httpd 走 httpd_uri.c:369 直接调 handler，
 *       此时该 session 的 ws_handshake_done == false；
 *   (b) WS 握手后的**数据帧** —— httpd 走 httpd_parse.c:796 分支，
 *       此时 ws_handshake_done == true。
 *
 * 判据（踩了两次坑才定下来，必须照抄）：
 *   - 不能读 Upgrade 头：httpd 进 handler 前已完成握手解析，数据帧阶段
 *     读不到 HTTP 头 → 曾对 WS 帧回 "HTTP/1.1 200 OK"，客户端协议层直接断开。
 *   - 不能靠 for_async_req：该标志只在主动调
 *     httpd_req_async_handler_begin() 时才设（httpd_txrx.c:708），握手后并不会置位。
 *   - 🔴 不能靠「fd 在不在订阅表里」（本项目曾这么写，是错的）：fd 会被 lwIP
 *     复用，僵尸订阅条目会让后来的**普通 GET** 被误判成 WS 数据帧，
 *     症状是 GET /api/v1/events 恒返回 0 字节、连接被直接关掉。
 *   - ✅ 唯一可靠且是公开 API 的判据：httpd_ws_get_fd_info(s_ev_srv, fd)
 *     直接问 httpd「这个 fd 现在是不是一个活跃 WS 会话」
 *     （httpd_ws.c: httpd_sess_get() + ws_handshake_done && !ws_close）。
 *     它在 handler 内调用会命中 httpd_sess_get 的「当前请求」快捷路径。
 */
    if (httpd_ws_get_fd_info(s_ev_srv, fd) != HTTPD_WS_CLIENT_WEBSOCKET) {
        /* 普通 GET：回订阅状态 JSON。注意此时**不能**调 httpd_ws_recv_frame
         * ——它要求 ws_handshake_done，会返回 ESP_ERR_INVALID_STATE；且我们本来
         * 就还在普通 HTTP 模式，直接回响应即可。*/
        char msg[160];
        int n = snprintf(msg, sizeof(msg),
            "{\"ok\":true,\"transport\":\"websocket\",\"subscribers\":%d,"
                "\"hint\":\"connect with Upgrade: websocket to subscribe\"}",
            s_ev_sub_count);
        if (n <= 0 || (size_t)n >= sizeof(msg)) return ESP_FAIL;
        httpd_resp_set_type(req, "application/json");
        httpd_resp_set_hdr(req, "Cache-Control", "no-store");
        httpd_resp_send(req, msg, n);
        return ESP_OK;   /* 正常响应：httpd 会自己关连接，语义正确 */
    }

    /* 到这里必定是已完成握手的 WS 会话。以下任何失败都意味着帧流已错位，
     * 绝不能再发 HTTP 响应（混入 HTTP 会破坏 WS 流），只能交给 httpd 关 socket。 */

    uint8_t buf[256];
    httpd_ws_frame_t f = {
   .final = true, .fragmented = false,
        .type = HTTPD_WS_TYPE_TEXT,
        /*🔴 frame->len **必须为 0** ——这是 httpd_ws_recv_frame_internal() 的入口条件
    * （`if (frame->len == 0) { 解析帧头/长度/mask key }`）。若预填非 0，
         * 它会跳过整个帧头解析，直接把 socket 后续字节当 payload 读 max_len 个，
         * 导致：① payload 是含 mask key 的密文；② 读多了字节破坏 TCP 流，
       * 客户端随即报 "reserved bits must be 0 / no close frame received"。
  * 缓冲容量要传给 **max_len 参数**，不是 frame->len。 */
     .payload = buf, .len = 0,
    };
esp_err_t ret = httpd_ws_recv_frame(req, &f, sizeof(buf));

    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "events: ws recv failed on fd %d (%s), closing", fd, esp_err_to_name(ret));
        ev_sub_del(fd);
        return ESP_FAIL;
    }

    /* 握手成功但第一帧没登记成功过 → 在此补登记。 */
    if (ev_sub_find(fd) < 0) ev_sub_add(fd);

  if (f.type == HTTPD_WS_TYPE_CLOSE) {
        ESP_LOGI(TAG, "events: client %d sent CLOSE", fd);
        ev_sub_del(fd);
        return ESP_FAIL;
    }

    if (f.type == HTTPD_WS_TYPE_TEXT && f.payload && f.len > 0) {
        buf[f.len < sizeof(buf) ? f.len : sizeof(buf) - 1] = '\0';
        ESP_LOGI(TAG, "events: rx from %d: %s", fd, (char *)buf);
        /* 收到任意文本都回一条 status（含设备当前状态），既当心跳也当握手确认：
         * httpd 原生 WS **不会**在握手后主动调一次 handler，所以客户端若只握手
         * 不发帧是收不到任何东西的；把确认绑在「第一帧」上是最可靠的握手验证。 */
        char ip[24] = "0.0.0.0";
        platform_wifi_ip(ip, sizeof(ip));
        char pong[224];
        int pn = snprintf(pong, sizeof(pong),
            "{\"type\":\"status\",\"echo\":\"%.*s\",\"device\":\"qshou\","
            "\"ip\":\"%s\",\"rssi\":%d,\"ble_connected\":%d,\"subscribers\":%d}",
            (int)f.len, (char *)buf, ip, platform_wifi_rssi(),
            platform_ble_is_connected() ? 1 : 0, s_ev_sub_count);
        if (pn > 0 && (size_t)pn < sizeof(pong)) {
            httpd_ws_frame_t pf = {
                .final = true, .fragmented = false,
                .type = HTTPD_WS_TYPE_TEXT,
                .payload = (uint8_t *)pong, .len = (size_t)pn,
            };
            httpd_ws_send_frame(req, &pf);
        }
    }
    return ESP_OK;
}

int platform_events_subscriber_count(void)
{
    return s_ev_sub_count;
}

int platform_events_broadcast(const char *json)
{
    if (!json || !s_ev_srv) return 0;
    /* ⚠️ 这里**故意不调 ev_sub_reap()**：本函数由相机/BLE/HTTP 等任意任务调用，
     * 而 httpd_sess_get() 只有在 httpd 任务内才走「当前请求」快捷路径，
     * 在外部会无锁遍历会话表（httpd_sess.c: httpd_sess_enum），并发调用有竞态。
     * 僵尸条目的回收统一放在 events_ws_handler 里（那里保证在 httpd 任务内）；
     * 代价只是两次请求之间 subscribers 可能虚高，不影响投递正确性 ——
     * 向已关闭的 fd 发帧只会让 httpd_ws_send_frame_async 返回错误。*/
    int sent = 0;
    for (int i = 0; i < EVENTS_SUB_MAX; i++) {
        const int fd = s_ev_fd[i];
        if (fd < 0) continue;
        httpd_ws_frame_t f = {
            .final = true, .fragmented = false,
            .type = HTTPD_WS_TYPE_TEXT,
            .payload = (uint8_t *)json, .len = strlen(json),
        };
        if (httpd_ws_send_frame_async(s_ev_srv, fd, &f) == ESP_OK) sent++;
        else ESP_LOGD(TAG, "events: async send to %d dropped (queue full)", fd);
    }
    return sent;
}

int platform_http_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    /* 26 个 API(见 ROUTES) + / (UI) + /stream + /snapshot + /events(WS) + 余量。
     * ⚠️ 加新端点时必须同步调大，否则 httpd_start 之后注册失败返回
     * HTTPD_ERR_HTTPD_HANDLERS_FULL，症状是该端点 404
     * "Nothing matches the given URI"（注意：不是我们的 routes 404）。 */
    cfg.max_uri_handlers = 32;   /* 编译期基础值（硬上限）；运行时按 NVS(qshou_cfg:http_max_uri) 钳制到 [need,32] */
    {
        /* + / + /stream + /snapshot + /api/v1/events（WS 单独注册，不在 ROUTES 里） */
        size_t need = sizeof(ROUTES) / sizeof(ROUTES[0]) + 4;
        uint8_t b = 0;
        if (nvs_cfg_get_u8("http_max_uri", &b) && b) {
            uint8_t v = b;
            if (v < need) v = need;   /* 硬钳制下限：必须装得下所有 handler */
            if (v > 32)   v = 32;     /* 硬钳制上限：防止 HTTP 服务起不来 */
            cfg.max_uri_handlers = v;
        }
    }
    /* 🔴 启用 URI 通配匹配：否则 gpio 单引脚路由（带 '*' 通配的那条）被当作字面串，
     *    永远匹配不上请求 → 404 "Nothing matches the given URI"。
     *    开启后精确路由仍走 httpd_uri_match_wildcard 的 strncmp 精确分支，不受影响。 */
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    cfg.lru_purge_enable = true;
    /* 开启 keep-alive：拉取式预览(/snapshot)与 API 复用同一 TCP 连接，
     * 避免每帧重新握手（之前手势接口 Connection:close 握手 ~2.6s 的同款坑）。 */
    cfg.keep_alive_enable   = true;
    cfg.keep_alive_idle     = 5;   /* 无数据 5s 后开始探活 */
    cfg.keep_alive_interval = 3;
    cfg.keep_alive_count    = 3;
    cfg.max_open_sockets    = 7;   /* HTTP server 硬上限 7（内部已占 3 个）；超限 → httpd_start 失败、整个 web 起不来 */

    httpd_handle_t srv = NULL;
    if (httpd_start(&srv, &cfg) != ESP_OK) return -1;
    s_ev_srv = srv;   /* 供 platform_events_broadcast 的异步发送使用 */

    for (size_t i = 0; i < sizeof(ROUTES) / sizeof(ROUTES[0]); i++) {
        httpd_uri_t u = {
            .uri      = ROUTES[i],
            .method   = HTTP_ANY,       /* 由 routes_handle 内部分派 GET/POST */
            .handler  = http_bridge,
            .user_ctx = NULL,
        };
        if (httpd_register_uri_handler(srv, &u) != ESP_OK) {
            ESP_LOGE(TAG, "register %s failed", ROUTES[i]);
            return -1;
        }
    }

    /* 设备端 Web UI：GET / → ui.html（text/html）*/
    httpd_uri_t ui = {
        .uri      = "/",
        .method   = HTTP_GET,
        .handler  = http_ui_handler,
        .user_ctx = NULL,
    };
    if (httpd_register_uri_handler(srv, &ui) != ESP_OK) {
        ESP_LOGE(TAG, "register / (ui) failed");
        return -1;
    }

    /* MJPEG 图传：GET /stream 只做 302 跳转到 81 端口（真正的图传在独立任务里，
     * 绝不能再让 while(1) 的 handler 占住 httpd 的唯一任务 —— 那会让所有
     * /api/v1/gesture 与 /api/v1/key 请求在推流期间完全得不到处理）。
     * GET /snapshot（单帧）是短请求，留在 80 端口的 httpd 上。 */
    httpd_uri_t stream = {
        .uri      = "/stream",
        .method   = HTTP_GET,
        .handler  = http_stream_redirect_handler,
        .user_ctx = NULL,
    };
    if (httpd_register_uri_handler(srv, &stream) != ESP_OK) {
        ESP_LOGE(TAG, "register /stream failed");
        return -1;
    }
    httpd_uri_t snap = {
        .uri      = "/snapshot",
        .method   = HTTP_GET,
        .handler  = http_snapshot_handler,
        .user_ctx = NULL,
    };
    if (httpd_register_uri_handler(srv, &snap) != ESP_OK) {
        ESP_LOGE(TAG, "register /snapshot failed");
        return -1;
    }

    /* WebSocket 事件通道：GET /api/v1/events。
     * 必须 HTTP_GET（否则握手不被处理）+ is_websocket=true；
     * 且不能放进上面的 ROUTES 循环（那里是 HTTP_ANY 桥接到 routes_handle）。 */
    httpd_uri_t events = {
        .uri      = "/api/v1/events",
        .method   = HTTP_GET,
        .handler  = events_ws_handler,
        .user_ctx = NULL,
        .is_websocket            = true,
        .handle_ws_control_frames = false,   /* PING/PONG/CLOSE 由 httpd 自己处理 */
    };
    if (httpd_register_uri_handler(srv, &events) != ESP_OK) {
        ESP_LOGE(TAG, "register /api/v1/events (ws) failed");
        return -1;
    }

    ESP_LOGI(TAG, "http server started (%u routes + ui + stream + snapshot + events)",
             (unsigned)(sizeof(ROUTES) / sizeof(ROUTES[0])));
    return 0;
}

/* ---- 推流启动：预热相机 + 拉起独立的 MJPEG 服务器任务 ----
 * 图传不再挂在 esp_http_server 上（原因见上方「MJPEG 图传」注释块），
 * 改由独立 socket 服务器 + 独立任务承载，端口 81；80 端口的 /stream 只做跳转。
 * 这里先做一次「预热取帧」，把相机初始化错误在启动日志里就暴露出来，
 * 而不是等到用户打开预览才发现。失败不致命（可能是相机未接 / 引脚不符）。 */
int platform_stream_start(void)
{
    const uint8_t *buf = NULL;
    size_t len = 0;
    if (platform_camera_capture(&buf, &len) == 0 && len > 0)
        ESP_LOGI(TAG, "camera warm-up ok (%u bytes/frame)", (unsigned)len);
    else {
        ESP_LOGW(TAG, "camera warm-up failed; stream will have no frames "
                      "until the camera/pins are fixed");
        platform_led_set_error(true);   /* 状态灯转红色常亮：启动故障可见化 */
    }

    /* 常驻的图传任务：优先级低于 httpd 与 gesture，且只阻塞自己。
     * 有客户端才发帧（accept 到连接才进推流循环），客户端断开即回到 accept。 */
    BaseType_t ok = xTaskCreate(mjpeg_stream_task, "mjpeg_stream",
                                MJPEG_STREAM_STACK, NULL,
                                MJPEG_STREAM_PRIO, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "mjpeg stream task create failed");
        return -1;
    }
    return 0;
}

#endif /* PLATFORM_ESP32 */
