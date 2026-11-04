/*
 * platform/platform.h — 硬件抽象层（接缝）
 *
 * 作用：把一切硬件相关调用收拢到这一层，使 firmware 的纯逻辑
 * （hid_report / mjpeg / http_api / swipe 插值）可在 PC 上用 gcc 直接
 * 编译与单元测试，无需 ESP32 开发板或 ESP-IDF 工具链。
 *
 * 两条实现：
 *   - platform/host/    : 本机桩（BLE 写入静态缓冲供测试读取、camera 返回合成 JPEG
 *                         标记、health 返回假值）。pre_arrival_work 阶段用。
 *   - platform/esp32/   : 到货后用 esp-idf / NimBLE / esp32-camera 实现。
 *
 * 编译宏：
 *   -DPLATFORM_HOST   选 host 桩
 *   -DPLATFORM_ESP32  选 esp32 实现（到货后）
 *   （两者都不定义时默认按 host 处理，保证 main/main.c 的
 *     #if defined(PLATFORM_ESP32) 分支不出错。）
 * 固件逻辑代码只允许调用本文件声明的 API，禁止直接 #include 任何
 * esp-idf / arduino / nimble 头文件。
 */

#ifndef QSHOU_PLATFORM_H
#define QSHOU_PLATFORM_H

#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * platform_ble_emit
 *   发射一个已经打包好的**鼠标** report（相对鼠标，3 字节）到已连接的中心设备。
 *   report : 指向 report 字节（[buttons][dX][dY]）。
 *   len    : 字节数（恒为 3）。
 *   返回 0 成功，-1 失败。
 *   host 桩：拷贝到最后发射缓冲并记录计数，供单元测试读取。
 *   esp32  ：经 esp_hidd_dev_input_set() 发送（Report ID 1）。
 */
int platform_ble_emit(const uint8_t *report, size_t len);

/*
 * platform_ble_emit_report
 *   通用发射：按 Report ID 路由到对应的 HID Report 特征（复合设备）。
 *   report_id : 1=鼠标(3B) / 2=键盘(8B) / 3=Consumer(2B)
 *   report/len: 已打包的**纯数据**（不带 Report ID 前缀 —— HOGP 靠特征关联）。
 *   返回 0 成功；未连接时返回 0（静默丢弃，属可恢复外部状态）；参数非法返回 -1。
 */
int platform_ble_emit_report(uint8_t report_id, const uint8_t *report, size_t len);

/*
 * platform_hid_set_emit —— HID 发射出口注入（装配层调用）
 *
 * 背景：手势回放队列与按键队列跑在**平台层的常驻任务**里（依赖 FreeRTOS 的
 *   串行队列语义，不能搬到纯逻辑层）。它们原先直发 BLE，导致 USB 后端接入后
 *   手势/按键仍只走 BLE —— 功能路径不一致。
 *
 * 做法：平台层内部发射统一经过 hid_emit_out()：注入了就调注入的函数，
 *   未注入则退回本平台直发 BLE（保持既有行为不变）。装配层（main.c）
 *   注入 hid_router_emit，于是手势/按键与 HTTP 端点走**同一条路由**。
 *
 * 递归说明：注入 hid_router_emit 后，router 的 BLE 后端正是
 *   platform_ble_emit_report（不经过本 hook）→ 天然无递归。
 * 传 NULL 恢复默认直发。
 */
typedef int (*platform_hid_emit_fn)(uint8_t report_id, const uint8_t *data, size_t len);
void platform_hid_set_emit(platform_hid_emit_fn fn);

/*
 * platform_usb_init —— USB HID 后端安装（Phase 1，见 docs/usb_hid_plan.md §12）
 *   在 ESP32-S3 的**原生 USB-OTG**（固定 GPIO19 = D- / GPIO20 = D+）上装 TinyUSB
 *   复合 HID 设备：接口 0 = Boot Keyboard（8B 无 Report ID）、
 *   接口 1 = 鼠标(RID 1) + Consumer(RID 2)。
 *
 *   ⚠️ 与 USB-Serial-JTAG 互斥：二者共享同一组 USB PHY（GPIO19/20），
 *      启用本模块必须关 CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG。
 *      主控制台是 UART0（CH343），日志与烧写不受影响；救砖走 UART 口。
 *   ⚠️ 要求 CONFIG_TINYUSB_HID_COUNT=2（两个 HID 实例）。
 *
 *   返回 0 成功（已安装，等主机枚举）；-1 失败（不致命 —— 调用方继续跑 BLE）。
 *   host 桩：空操作，返回 0（保证零硬件可构建）。
 */
int platform_usb_init(void);

/*
 * platform_usb_ready —— USB 后端当前是否可用（= 主机已枚举）。
 *   供 hid_router 的 ready 回调使用：1 = 可用 / 0 = 未挂载（回落 BLE）。
 *   host 桩恒返回 0。
 */
int platform_usb_ready(void);

/*
 * platform_usb_emit —— USB 后端发射（hid_router 的 send 回调）。
 *   report_id / data / len 的语义与 platform_ble_emit_report **完全一致**
 *   （BLE 语义 Report ID + 不带前缀的纯数据）；本函数内部用 usb_hid_pack()
 *   转成 USB 线帧（接口号 + Report ID 前缀），再经 TinyUSB 发出。
 *
 *   返回 0 成功；-1 失败（未挂载 / 未知 RID / 长度不符 / FIFO 忙 / 未就绪）。
 *   ⚠️ 失败**不要**重试或换后端 —— 见 hid_router.h 铁律 1（严禁双发）。
 *   host 桩恒返回 -1。
 */
int platform_usb_emit(uint8_t report_id, const uint8_t *data, size_t len);

/*
 * platform_camera_capture
 *   取一帧 JPEG。*buf 指向内部缓冲（调用方须在下次调用前拷贝），
 *   *len 为字节数。返回 0 成功，-1 失败。
 *   host 桩：返回带 SOI/EOI 标记的合成小缓冲。
 */
int platform_camera_capture(const uint8_t **buf, size_t *len);

/*
 * platform_uptime_ms / free_heap / wifi_rssi / reset_reason
 *   health 端点所需的遥测。host 桩返回确定性的假值。
 *
 * 🔴 platform_free_heap 的口径：esp_get_free_heap_size() = **所有 region 之和**
 *   （内部 DRAM + PSRAM）。故 health.free_heap 是「总可用」，与
 *   sysinfo.mem.heap_free（仅内部 RAM）**不是同一口径**，两者相差一个 psram_free。
 *   恒等式：health.free_heap ≈ sysinfo.mem.heap_free + sysinfo.mem.psram_free。
 *   前端已分别标注为「总堆」与「内部堆」，不要把它们当同一个数字比较。
 */
uint32_t     platform_uptime_ms(void);
uint32_t     platform_free_heap(void);
int          platform_wifi_rssi(void);
const char  *platform_reset_reason(void);

/*
 * platform_log
 *   带 tag 的日志（printf 风格）。host 桩走 stdout。
 */
void platform_log(const char *tag, const char *fmt, ...);

/*
 * ---- 网络 / 系统接缝（platform_net.c）----
 *
 * 作用：把 ESP32 的启动序列（NVS / WiFi / BLE 栈 / HTTP server / 推流任务）
 * 全部收拢到这里，使 main/main.c 成为**零 ESP-IDF 依赖**的纯编排层。
 * 这样无论 host 还是 esp32，main.c 都能编译，且到货后只需替换 platform/esp32。
 *
 * 返回值统一 0 成功 / -1 失败。
 */

/* 初始化 NVS（WiFi 凭据、device_id、标定 profile 的存储） */
int platform_system_init(void);

/* 启动 BLE HID（esp32: NimBLE + Absolute Mouse 描述符；host: 空操作） */
int platform_ble_init(const char *device_name);

/* 连接 WiFi（凭据由 platform 内部从 NVS 读；host: 空操作）
 * 注意：本函数只负责启动 STA 并注册事件回调，实际连接由 WiFi 事件驱动，
 * 返回 0 不代表已拿到 IP。要判断是否在线请用 platform_wifi_is_connected。 */
int platform_wifi_init(void);

/* STA 是否已关联并拿到 IP（esp32: 由 IP_EVENT_STA_GOT_IP 置位；host: 恒 false） */
bool platform_wifi_is_connected(void);

/* BLE HID 是否已与中心设备建立连接（esp32: 由 GAP 事件维护；host: 恒 false） */
bool platform_ble_is_connected(void);

/* 上一次手势回放的可观测量（esp32: 真实值；host: 全 0）。
 * frames = 实际发射帧数；dx/dy = 设备端认为的净位移（box/circle 应为 0）。
 * 用途：区分「固件几何算错」与「host 侧丢帧 / 渲染问题」。 */
void platform_ble_last_gesture(int *frames, int *dx, int *dy);

/* 上一次手势的帧节奏抖动（ms）：相邻两帧发出时刻的最小/平均/最大间隔。
 * 用于区分「点距不一致」来自固件调度（应仅 ±1ms）还是 BLE 连接间隔量化。
 * host: 全 0。 */
void platform_ble_gesture_jitter(int *min_ms, int *avg_ms, int *max_ms);

/* 上一次手势的发送计数：sent = 真正发出去的帧数，drop = 重试后仍失败的帧数。
 * drop > 0 即「点距不一致」的直接证据（丢一帧 → 该处点距翻倍）。
 * 注意 net_dx/dy 只累加 sent 的帧，故与空中实际位移口径一致。host: 全 0。 */
void platform_ble_gesture_sent(int *sent, int *drop);

/* 注册 6 个 /api/v1/ 端点并启动 HTTP 服务器 */
int platform_http_start(void);

/* 启动 MJPEG 推流任务（host: 空操作） */
int platform_stream_start(void);

/*
 * ---- 设备配置 NVS（ns qshou_cfg，冷生效：写后 esp_restart）----
 *
 * 供设备端 Web UI（/api/v1/settings）读写。所有项均为「冷生效」：
 * 写入后由调用方触发 platform_request_reboot()，boot 时各接缝从 NVS 读取应用。
 * get 返回 1=找到并填入，0=未找到/出错（调用方应回退到编译期默认值）。
 * ⚠️ WiFi 凭据单独走 ns "qshou"（与 BLE 绑定 ns 分开，写 WiFi **绝不**触碰 bonding）。
 */
int  platform_cfg_get_str(const char *key, char *out, size_t n);
int  platform_cfg_get_u8 (const char *key, uint8_t *out);
int  platform_cfg_get_u16(const char *key, uint16_t *out);
int  platform_cfg_set_str(const char *key, const char *val);
int  platform_cfg_set_u8 (const char *key, uint8_t val);
int  platform_cfg_set_u16(const char *key, uint16_t val);
int  platform_cfg_set_wifi(const char *ssid, const char *pass);  /* 写 ns qshou，pass 空=不改 */
void platform_request_reboot(void);   /* esp32: esp_restart；host: 空操作 */

/*
 * ---- 控制 API（面向程序 / AI Agent；REST + /api/v1/schema 自描述）----
 *
 * 设计：所有"动作"默认经 Bearer 鉴权钩子（g_control_auth，默认关，LAN 内使用），
 * 状态均可 GET 读、POST 改。新增硬件能力先入本层（host 桩 + esp32 实现），
 * routes.c 只做编排与 JSON。GPIO 安全白名单见 platform_gpio.c。
 */
typedef enum {
    GPIO_MODE_IN  = 0,   /* 数字输入 */
    GPIO_MODE_OUT = 1,   /* 数字输出（value 0/1） */
    GPIO_MODE_PWM = 2    /* PWM 输出（value=占空比 0..255） */
} gpio_mode_e;

typedef struct {
    int         pin;     /* GPIO 编号（仅白名单内可管理） */
    gpio_mode_e mode;    /* 当前模式 */
    int         value;   /* OUT:0/1  PWM:0..255  IN:0/1 */
} gpio_pin_info_t;

/* 列出可管理引脚及其状态（白名单内）。out 最多 max 个；返回实际数量，-1=错误。 */
int platform_gpio_list(gpio_pin_info_t *out, int max);
/* 读取单引脚状态（pin 须可管理）；不可管理返回 -1。 */
int platform_gpio_get(int pin, gpio_pin_info_t *info);
/* 设模式（白名单校验，危险引脚返回 -1）。 */
int platform_gpio_pin_mode(int pin, gpio_mode_e mode);
/* 写输出（OUT: value 0/1）。 */
int platform_gpio_write(int pin, int value);
/* 写 PWM 占空比（PWM: 0..255）。 */
int platform_gpio_pwm(int pin, int duty);
/* 读输入电平（IN 模式）：0/1；非 IN 返回 -1。 */
int platform_gpio_read(int pin);
/* 全局 PWM 频率（Hz），0=驱动默认。 */
int platform_gpio_pwm_set_freq(int hz);

/* ---- WiFi 实时管理（platform_net.c）---- */
typedef struct {
    char ssid[33];
    int  rssi;
    char bssid[18];
    int  auth;     /* 0=OPEN 1=WEP 2=WPA_PSK 3=WPA2_PSK 4=WPA_WPA2 5=WPA3 ... */
} wifi_ap_t;

/* 热连接指定 SSID（pass 可空）；不重启。返回 0 已发起连接。 */
int  platform_wifi_connect(const char *ssid, const char *pass);
/* 触发一次扫描；结果经 platform_wifi_scan_results 取。返回 0 已发起。 */
int  platform_wifi_scan(void);
/* 取最近一次扫描结果，out 最多 max 个；返回 AP 数（0 表示暂无）。 */
int  platform_wifi_scan_results(wifi_ap_t *out, int max);
/* 断开当前 STA 连接。 */
int  platform_wifi_disconnect(void);
/* 把当前 IP 写入 buf（点分十进制，如 "192.168.1.5"）；未连接写 "0.0.0.0"。 */
int  platform_wifi_ip(char *buf, size_t n);

/* ---- BLE 外设态管理（platform_ble.c）---- */
/* 开关广播（设备作为 HID 外设）。 */
int  platform_ble_advertise(bool on);
/* 断开与当前中心的连接。 */
int  platform_ble_disconnect(void);
/* 把已配对列表写成 JSON 数组字符串填入 buf（如 [{"id":1,"addr":"AA:BB:.."}]）。 */
int  platform_ble_list_bonds(char *buf, size_t n);
/* 遗忘指定 id 的配对。 */
int  platform_ble_forget_bond(int id);
/* 是否正在广播。 */
bool platform_ble_is_advertising(void);

/* ---- 事件推送接缝（platform_events.c；esp32 用 httpd 原生 WebSocket）----
 * 向所有已连接的 /api/v1/events 订阅者广播一条 JSON 文本事件。
 * 非阻塞：内部对每个订阅者调用 httpd_ws_send_frame_async()，
 * 若 httpd 控制队列已满则**丢弃该条事件**（不阻塞调用方，
 * 因为 CONFIG_HTTPD_QUEUE_WORK_BLOCKING 故意未开）。
 * 返回成功投递的订阅者数；0 表示当前无订阅者或全部投递失败。 */
int platform_events_broadcast(const char *json);
/* 当前 /api/v1/events 订阅者数量。 */
int platform_events_subscriber_count(void);

/*
 * ---- 状态灯接缝（platform_led.c）----
 *
 * 板载 WS2812 RGB（GPIO48）+ IO2 单色用户灯，替代串口日志做「可视化状态输出」
 * （BLE 测试期间禁开串口 —— DTR 下降沿会复位板子）。
 *
 * 六态协议（WS2812）：
 *   红常亮 = 启动故障（platform_led_set_error）
 *   琥珀慢闪1Hz = WiFi 连接中；蓝慢闪1Hz = BLE 广播中
 *   绿常亮 = BLE 已连接空闲；青常亮 = 推流中；白短闪80ms = HID 活动叠加
 * IO2 灯 = BLE 已连接硬指示（二值、远距离可辨，与 WS2812 双通道确认）。
 *
 * platform_led_init 由 main.c 在启动序列最前调用（host: 空操作）；
 * streaming/error 标志由 platform_net.c / 各初始化环节按需置位。
 * wifi / ble / HID 活动状态由 LED 任务内部自行读取对应接缝 API，无需外部喂。
 */
void platform_led_init(void);
void platform_led_set_streaming(bool on);
void platform_led_set_error(bool on);

/* IO2 调试接缝（BLE 测试期间不可开串口，只能靠 HTTP 读数）：
 * platform_led_set_io2(duty)  手工强制 IO2 输出，-1 = 恢复自动（BLE 已连接指示）。
 *                             - GPIO 直驱模式（默认）：duty>0 → 强制「亮」，
 *                               duty=0 → 强制「灭」（已按有效极性换算）；
 *                             - PWM 对照模式：duty 为原始占空比 0..255，
 *                               判极性：duty=255 亮=高有效；duty=0 亮=低有效。
 * platform_led_set_io2_pol(pol) 运行时覆盖极性（1=高有效、0=低有效、-1=恢复编译期
 *                              宏）。判极性猜错时用它即时翻转，**不必重烧**。
 * platform_led_io2_diag(...)  回读 LEDC 是否初始化成功，以及启动时 GPIO2 pad
 *                             自检读到的高/低电平（-1=未测）。
 *                             pad_high=1 & pad_low=0 = GPIO 正常控制 pad；
 *                             两者同值 = pad 被外部拉死，IO2 软件上不可用。
 *                             *pad_now = 最近一 tick 读到的 pad 实时电平(0/1/-1)；
 *                             *pad_pct = 最近一 tick 的 pad 高电平占比 0..100
 *                                       （LED 任务里连采 64 次估算占空比，
 *                                       用来判断 LEDC 是否真把 PWM 打到了 pad 上：
 *                                       duty=255→应≈100，duty=0→应≈0，duty=55→应≈22）。
 * platform_led_set_io2_mode(m) IO2 驱动方式对照开关（**纯诊断用**，排查「不亮」时
 *                              必须能把 LEDC 与 GPIO 直驱分开判）：
 *                              m=1  → GPIO 直驱（ledc_stop + 释放 pad + gpio_set_level）
 *                              m=0  → 回到 LEDC PWM
 *                              同一現象下「GPIO 模式能亮、PWM 模式不亮」即可钉死
 *                              为 LEDC 侧问题（占用/时钟/pad 路由），与极性无关。 */
void platform_led_set_io2(int duty);
void platform_led_set_io2_pol(int pol);
void platform_led_set_io2_mode(int gpio_mode);
int  platform_led_io2_mode_get(void);   /* 回读当前 IO2 驱动方式：1=GPIO 直驱，0=LEDC PWM */
void platform_led_io2_diag(int *ready, int *pad_high, int *pad_low,
                           int *pad_now, int *pad_pct);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_PLATFORM_H */
