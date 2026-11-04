/*
 * platform/esp32/platform_ble.c — ESP32 BLE HID 接缝实现
 *
 * 路线决策（见 docs/dependency_licenses.md 的 R18）：
 *   采用 **ESP-IDF 自带 esp_hid 组件 + NimBLE transport**，描述符自持。
 *   —— 不依赖 Autfy/ESP32-BLE-HID 等 Arduino 生态第三方库，规避其许可证
 *      不确定性（R18 缺口）；HID over GATT 的 GATT 细节（HID Service、
 *      Report Map / Report / Protocol Mode / HID Control Point、boot 协议、
 *      配对与 bond 管理）全部由官方组件承担。
 *
 * 为什么能这么走（已核实 ESP-IDF v6.1 源码）：
 *   components/esp_hid/CMakeLists.txt 在 CONFIG_BT_NIMBLE_ENABLED 时会编译
 *   src/nimble_hidd.c —— 即 **NimBLE 下的 HID Device 是官方支持的**。
 *   该实现通过 NimBLE 的 ble_svc_hid（services/hid/ble_svc_hid.h）注册
 *   HID GATT 服务，受 CONFIG_BT_NIMBLE_HID_SERVICE 门控
 *   （本项目 sdkconfig.defaults 已开启）。
 *
 * 描述符来源：ble_hid/hid_descriptor.c（Relative Mouse，纯逻辑，host 共用）。
 * 3 字节 report 布局见 ble_hid/hid_report.h。
 *
 * 契约（platform.h）：
 *   platform_ble_emit —— 哑中继出口，不做任何语义解析（R20 密文约束）。
 */
#ifdef PLATFORM_ESP32

#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "platform/platform.h"
#include "ble_hid/hid_report.h"
#include "ble_hid/hid_descriptor.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_event.h"
#include "esp_hidd.h"
#include "esp_hid_common.h"

#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "ble_hid/ble_hid.h"   /* gesture_spec_t / ble_hid_build_gesture_points */

/* ── NimBLE 协议栈前置初始化所需 ──────────────────────────────────
 * esp_hid 的 NimBLE transport（src/nimble_hidd.c）**只负责注册 HID GATT**，
 * 它依赖 NimBLE host 已就绪（ble_hs_cfg 可用），但**自身不启动协议栈**。
 * 若不在此处完成 controller + host 初始化，esp_hidd_dev_init() 在真机上
 * 会失败（controller 未 enable），表现为 BLE 广播不出现。
 * 序列照搬 IDF 官方 examples/bluetooth/esp_hid_device/main/esp_hid_gap.c
 * 的 init_low_level()：mem_release(CLASSIC_BT) → nimble_port_init()
 *   → nimble_port_freertos_init(host_task)。 */
#include "esp_bt.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

/* ── 广播（advertising）所需 ──────────────────────────────────────
 * 🔴 真机实测发现的关键缺口：esp_hid 的 NimBLE transport（nimble_hidd.c）
 * **只注册 HID GATT 服务，不做任何广播**（grep 该文件无 ble_gap_adv_* 调用）。
 * 而 IDF 官方 examples/bluetooth/esp_hid_device 是在 esp_hidd_dev_init()
 * 之后显式调用 esp_hid_ble_gap_adv_init() + esp_hid_ble_gap_adv_start() 的。
 * 本模块原先漏了这一步 → 真机日志只有 "GAP procedure initiated: stop
 * advertising."，**永远看不到 start advertising** → 中心端搜索不到设备。
 * 下列实现照搬官方示例的 NimBLE 分支。 */
#include "host/ble_hs.h"
#include "host/ble_hs_adv.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "nimble/ble.h"

/* ── 安全 / 绑定（配对持久化）所需 ────────────────────────────────
 * 真机实测（2026-10-01）：设备重启后安卓"已配对"列表里丢失本设备、
 * 开机不能自动连接，必须重新搜索并手动配对。根因有二：
 *   1) 从未设置 ble_hs_cfg.sm_* → 根本没做 bonding（只建立了临时连接）；
 *   2) 项目 sdkconfig 未开 CONFIG_BT_NIMBLE_NVS_PERSIST → 即便 bonding
 *      发生，长期密钥也只存 RAM，掉电即失。
 * 下列头文件提供 sm 配置、GAP 安全事件处理与删除旧绑定的工具。 */
#include "host/ble_gap.h"
#include "host/ble_sm.h"
#include "host/ble_store.h"   /* ble_store_util_delete_peer */

#define TAG "QSHOU_BLE"

/* 读取/列举 bond 时一次性取回的 peer 上限。HID 外设典型只绑 1~2 台主机，
 * 但留出余量以便 /ble/bonds 能完整列出（一次栈上数组，8 个 × 7B，可忽略）。 */
#define QSHOU_BOND_MAX 8

/* ── HID 设备标识 ──────────────────────────────────────────────
 * VID/PID 说明：这是**开发阶段占位值**，不指向任何已注册厂商。
 * 到货联调时按规格 input.ble_hid 的记录确认最终值（避免与中心端
 * 已配对的其它 HID 设备冲突）。集中在此处便于单点修改。
 * TODO(vector): 与规格对齐最终 VID/PID/版本号。
 */
#define QSHOU_HID_VID        0x303A   /* Espressif 分配给自研样机的临时段，联调前复核 */
#define QSHOU_HID_PID        0x0001
#define QSHOU_HID_VERSION    0x0100   /* 1.0.0 */
#define QSHOU_HID_MANUF      "Qshou"
#define QSHOU_HID_SERIAL     "QSHOU-0001"

/* 唯一的 report map（Relative Mouse）在数组中的下标 */
#define QSHOU_HID_MAP_INDEX  0

static esp_hidd_dev_t *s_dev = NULL;
static volatile bool   s_connected = false;

/* ── 控制平面状态（P3）─────────────────────────────────────────────
 * s_conn_handle —— 当前连接的 HCI handle。在 ble_gap_event_cb 的 CONNECT
 *   事件记录，供 platform_ble_disconnect() 主动断连用（esp_hidd 的
 *   esp_hidd_dev_t 不暴露 handle，只能从 GAP 事件捕获）。
 * s_adv_active  —— 广播真实运行态。仅靠 flag 不可靠（连接建立时协议栈自动
 *   停广播、断开时又自动重启），故以「谁在驱动广播」为准：
 *     ADV_AUTO ：模块自动（boot 后由 host_synced 启动、断开后自动重启）
 *     ADV_OFF  ：用户经 /ble/advertise {"on":0} 手动关闭 → 断开后**不**自动重启
 *   is_advertising() 只读上报真实态，不改这个驱动源。
 * s_adv_owned_by_user —— 用户显式接管过广播控制权后，boot 的默认启动需让位。 */
typedef enum { ADV_AUTO = 0, ADV_OFF } adv_ctrl_t;
static volatile uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static volatile adv_ctrl_t s_adv_ctrl    = ADV_AUTO;

/* HID 最近一次成功发射的时间戳（ms，来自 esp_timer）。推流循环据此判断是否
 * 处于「HID 活动窗口」内：窗口内暂停发帧，把 2.4G 时隙让给 BLE，消除推流时
 * 鼠标/键盘数秒级延迟。由 platform_ble_last_tx_ms() 暴露给 platform_net.c。 */
static uint32_t s_last_hid_tx_ms = 0;

/* Absolute Mouse descriptor：指向纯逻辑模块，避免字节重复定义（单一真源） */
static esp_hid_raw_report_map_t s_report_maps[] = {
    {
        .data = qshou_hid_report_map,
        .len  = 0,   /* 运行期填 qshou_hid_report_map_len（const 初始化限制） */
    },
};

/* ── HID 事件回调：仅做最少的状态跟踪与日志 ──────────────────── */
static void hid_event_handler(void *handler_args, esp_event_base_t base,
                              int32_t id, void *event_data)
{
    (void)handler_args; (void)base;
    esp_hidd_event_data_t *p = (esp_hidd_event_data_t *)event_data;
    switch ((esp_hidd_event_t)id) {
    case ESP_HIDD_START_EVENT:
        ESP_LOGI(TAG, "HID stack started");
        break;
    case ESP_HIDD_CONNECT_EVENT:
        s_connected = true;
        ESP_LOGI(TAG, "HID connected");
        break;
    case ESP_HIDD_DISCONNECT_EVENT:
        s_connected = false;
        ESP_LOGW(TAG, "HID disconnected");
        break;
    case ESP_HIDD_PROTOCOL_MODE_EVENT:
        ESP_LOGI(TAG, "protocol mode: %u",
                 p ? (unsigned)p->protocol_mode.protocol_mode : 0u);
        break;
    case ESP_HIDD_CONTROL_EVENT:
        ESP_LOGI(TAG, "control: %u",
                 p ? (unsigned)p->control.control : 0u);
        break;
    case ESP_HIDD_OUTPUT_EVENT:
    case ESP_HIDD_FEATURE_EVENT:
        /* 本设备对中心端是只输出（notify），不需处理；仅记录 */
        ESP_LOGD(TAG, "out/feature report id=%u len=%u",
                 p ? (unsigned)p->output.report_id : 0u,
                 p ? (unsigned)p->output.length : 0u);
        break;
    case ESP_HIDD_STOP_EVENT:
        s_connected = false;
        ESP_LOGW(TAG, "HID stopped");
        break;
    default:
        break;
    }
}

/* ── NimBLE 协议栈启动（幂等）──────────────────────────────────────
 * 必须在 esp_hidd_dev_init() 之前调用。序列：
 *   1) esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT)
 *      —— 本项目只用 BLE，释放经典蓝牙内存（S3 上 CLASSIC 本就不可用，
 *         此调用在部分 IDF 版本会返回 ESP_ERR_INVALID_ARG，按官方示例忽略）。
 *   2) nimble_port_init() —— 一站式：初始化 controller + NimBLE host 栈
 *      （内部含 esp_bt_controller_init/enable 与 ble_hs 初始化）。
 *   3) nimble_port_freertos_init(host_task) —— 启动 NimBLE host task，
 *      host_task 内跑 nimble_port_run()（协议栈主循环，正常不返回）。
 * 注意：**不是** esp_nimble_init()（该符号在 IDF v6.1 不存在；
 *       未声明函数会被 C 隐式声明而不报错，是预检无法捕获的假阳性陷阱）。
 */
static bool s_bt_inited = false;

/* NimBLE host task 主体：跑协议栈主循环（对照 IDF 官方示例的 nimble_host_task） */
static void nimble_host_task(void *param)
{
    (void)param;
    nimble_port_run();          /* 阻塞于此，直到 nimble_port_stop() */
    nimble_port_freertos_deinit();
}

/* ble_adv_start() / ble_adv_set_fields() 定义在本文件后半段，sync 回调要用 */
static int ble_adv_start(void);
static int ble_adv_set_fields(const char *device_name);

/* 设备名缓存：广播字段要在 host sync 之后才设置（ble_gap_adv_set_fields 在
 * host 未 enable 时返回 BLE_HS_EDISABLED=30），而 sync 回调拿不到 cfg，
 * 故在此留存一份。 */
static char s_ble_name[64];

/* 🔴🔴 本模块最严重的时序坑（真机实测定位，2026-09-30）🔴🔴
 *
 * 症状：中心端能搜到、能配对、能连上（GAP connect established + HID connected），
 *       但设备永远「不活跃」——日志里从不出现 GAP subscribe，任何 input report
 *       都报 `ATT read: invalid handle; attr_handle=0x0000`
 *       + `NIMBLE_HIDD: Unable to fetch protocol_mode` + `input_set failed: ESP_FAIL`。
 *
 * 根因：**HID GATT 服务根本没有被注册进 GATT 数据库**，所有属性句柄都是 0。
 *
 * 机理（已核对 IDF v6.1 源码，逐条可查）：
 *   1) esp_hidd_dev_init() → nimble_hid_start_gatts() → ble_svc_hid_init()
 *      最终调到的只是 **ble_gatts_add_svcs()**（ble_gatts.c:3354）。
 *      这个函数**只把服务定义追加进待注册列表**，不分配句柄。
 *   2) 真正分配句柄、并触发 ble_hs_cfg.gatts_register_cb（从而让 nimble_hidd.c
 *      记下 hid_protocol_handle / report handle）的，是 **ble_gatts_start()
 *      （ble_gatts.c:1851，内部 ble_gatts_register_svcs，见 1956 行）**。
 *   3) ble_gatts_start() 由 ble_hs_start()（ble_hs.c:772→815）在 **host sync**
 *      时调用一次，且**只调用一次**。
 *   4) 若 host task（nimble_port_freertos_init）先跑起来并完成 sync，
 *      之后才 esp_hidd_dev_init() —— 那些服务定义就被追加进了一个**已经 start
 *      过**的数据库，再也没人注册它们。ble_gatts_mutable() 此时仍返回 true
 *      （无广播/无连接），所以 add_svcs 不报错、不 assert，**静默失败**。
 *      → 句柄全 0 → 中心端看不到任何 HID 特征 → 从不订阅 → 永不活跃。
 *
 * 判定标记：**日志里没有 "HID stack started"**。该行由 esp_hid 的
 * nimble_host_synced() 在 sync 时投递 ESP_HIDD_START_EVENT 产生；host 若已
 * 先于 esp_hidd_dev_init() sync，这个回调永远不会被装上去，于是永远不打印。
 *
 * 正确顺序（照抄 IDF 官方 examples/bluetooth/esp_hid_device/main/
 * esp_hid_device_main.c：esp_hidd_dev_init() 在 944 行，
 * nimble_port_freertos_init() 在 971 行，官方原注释即
 * "Starting nimble task after gatts is initialized"）：
 *      nimble_port_init()  →  esp_hidd_dev_init()  →  nimble_port_freertos_init()
 *
 * 因此下面拆成两段：bt_stack_pre_init() 只做 controller+host 内存池就绪，
 * bt_stack_start_host_task() 必须在 esp_hidd_dev_init() **之后**调用。
 */
static int bt_stack_pre_init(void)
{
    if (s_bt_inited)
        return 0;

    /* 只用 BLE：释放经典蓝牙占用的内存。部分 IDF 版本在 S3 上返回非 OK，
     * 属预期（S3 无经典蓝牙），不视为致命。 */
    esp_err_t rel = esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT);
    if (rel != ESP_OK && rel != ESP_ERR_INVALID_ARG) {
        ESP_LOGW(TAG, "mem_release(CLASSIC_BT) returned %s (ignored)",
                 esp_err_to_name(rel));
    }

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %s", esp_err_to_name(err));
        return -1;
    }

    /* ⚠️ 不要在此手动调用 ble_store_config_init()！
     * 该函数首行是 SYSINIT_ASSERT_ACTIVE()，**只能由 Newt sysinit 阶段调用**，
     * 而 sysinit 已随 nimble_port_init() 自动执行（绑定存储正是在那时挂接
     * store_read/write/delete 回调）。手动调用会触发断言复位。
     * 是否把 bond 持久化到 NVS 完全由编译期 CONFIG_BT_NIMBLE_NVS_PERSIST
     * 决定（见 sdkconfig.defaults），运行期无法切换。 */

    s_bt_inited = true;
    ESP_LOGI(TAG, "NimBLE host prepared (controller + host, task NOT started yet)");
    return 0;
}

/* 启动 NimBLE host task —— 必须在 esp_hidd_dev_init() 之后，见上注释。 */
static int bt_stack_start_host_task(void)
{
    nimble_port_freertos_init(nimble_host_task);
    ESP_LOGI(TAG, "NimBLE host task started (GATT DB will be registered on sync)");
    return 0;
}

/* host sync 回调链：esp_hidd_dev_init() 会把 ble_hs_cfg.sync_cb 换成它自己的
 * nimble_host_synced()。我们在其**之后**再挂一层，用于 sync 完成后启动广播 ——
 * ble_gap_adv_start() 必须等 host sync 之后才允许调用（官方示例同样是在 sync
 * 后才 adv_start）。不保存并回调前一个会导致 "HID stack started" 永不打印。 */
static void (*s_prev_sync_cb)(void) = NULL;

static void qshou_host_synced_cb(void)
{
    if (s_prev_sync_cb) {
        s_prev_sync_cb();   /* esp_hid 的 nimble_host_synced() → ESP_HIDD_START_EVENT */
    }

    /* 广播字段与广播启动必须成对出现在 sync 之后：
     * ble_gap_adv_set_fields() 在 host 未 enable 时返回 BLE_HS_EDISABLED(30)。
     * 官方 esp_hid_ble_gap_adv_start() 也是把这两步绑在一起做的。 */
    ESP_LOGI(TAG, "host synced; setting adv fields and starting advertising");
    if (ble_adv_set_fields(s_ble_name) != 0) {
        ESP_LOGE(TAG, "advertising field setup failed; device will NOT be discoverable");
        return;
    }
    if (ble_adv_start() != 0) {
        ESP_LOGE(TAG, "advertising start failed; device will NOT be discoverable");
    }
}

/* ── BLE 广播：设置报文 + 启动广播 ────────────────────────────────
 * HID GATT 服务 UUID（0x1812）必须出现在广播里，中心端才能识别这是
 * HID 设备。appearance 声明为 **Generic HID（0x03C0）**：本设备是复合
 * HID（相对鼠标 RID1 + 键盘 RID2 + Consumer RID3），中心端（安卓）应
 * 依据 HID 描述符解析出全部输入设备，而非被 pigeonhole 成单一鼠标。
 * 实现参照 IDF 官方 examples/bluetooth/esp_hid_device/main/esp_hid_gap.c
 * 的 `#if CONFIG_BT_NIMBLE_ENABLED` 分支。 */

#define QSHOU_GATT_SVC_HID_UUID  0x1812

static struct ble_hs_adv_fields s_adv_fields;

/* GAP 事件回调：本模块只需记录连接/断开，其余交由 esp_hid 内部处理。 */
static int ble_gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        ESP_LOGI(TAG, "GAP connect: %s (status=%d)",
                 event->connect.status == 0 ? "established" : "failed",
                 event->connect.status);
        /* 连接建立后广播自动停止。
         *
         * ⚠️ 必须主动请求低连接间隔（HOGP 规范做法），否则默认间隔
         * （常见 30~50ms，甚至 7.5ms 起步后被中心端改大）不会整除
         * gesture 的 step_ms，notify 被量化到不同数量的连接事件上：
         *   周期在「n×itvl」与「(n+1)×itvl」之间交替 → 帧时间轴抖动
         *   → host 侧指针加速随速度变化 → 屏幕上「点距不一致」。
         * 请求 itvl = 15ms（12×1.25ms，HOGP 推荐值）：90ms 正好是 6 个
         * 连接事件，帧节奏严格对齐，量化误差归零。 */
        if (event->connect.status == 0) {
            /* 记录连接 handle：供 platform_ble_disconnect() 主动断连使用
             * （esp_hidd 不对外暴露 handle，只能在此捕获）。 */
            s_conn_handle = event->connect.conn_handle;
            struct ble_gap_upd_params up = {
                .itvl_min            = 12,   /* 12 × 1.25ms = 15ms */
                .itvl_max            = 12,   /* 固定 15ms，节奏可预测 */
                .latency             = 0,    /* HID 不允许 slave latency */
                .supervision_timeout = 400,  /* 400 × 10ms = 4s */
            };
            int rc = ble_gap_update_params(event->connect.conn_handle, &up);
            if (rc != 0)
                ESP_LOGW(TAG, "conn param update request failed; rc=%d", rc);
            else
                ESP_LOGI(TAG, "requested conn itvl 15ms (HOGP)");
        }
        return 0;
    case BLE_GAP_EVENT_CONN_UPDATE:
        /* 注意：conn_update 事件体只有 status/conn_handle；实际生效的连接
         * 参数要用 ble_gap_conn_find 取 desc 才能读到（desc 才有 itvl/latency）。 */
        ESP_LOGI(TAG, "GAP conn updated: status=%d", event->conn_update.status);
        {
            struct ble_gap_conn_desc d;
            if (ble_gap_conn_find(event->conn_update.conn_handle, &d) == 0)
                ESP_LOGI(TAG, "  actual conn: itvl=%u (×1.25ms) latency=%u",
                         (unsigned)d.conn_itvl, (unsigned)d.conn_latency);
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGW(TAG, "GAP disconnect (reason=%d); re-advertising",
                 event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        /* 断开后重新开始广播，让中心端能再次发现本设备。
         * ⚠️ 但若用户经 /ble/advertise {"on":0} 显式关了广播（s_adv_ctrl
         *    == ADV_OFF），则**不**自动重启 —— 否则用户的关闭意图会被
         *    这次自动重启覆盖，「关广播」根本关不掉。 */
        if (s_adv_ctrl == ADV_OFF) {
            ESP_LOGI(TAG, "advertising suppressed (user turned it off)");
            return 0;
        }
        {
            struct ble_gap_adv_params adv_params;
            memset(&adv_params, 0, sizeof adv_params);
            adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
            adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
            int rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL,
                                       BLE_HS_FOREVER, &adv_params,
                                       ble_gap_event_cb, NULL);
            if (rc != 0)
                ESP_LOGE(TAG, "re-advertise failed; rc=%d", rc);
            else
                ESP_LOGI(TAG, "re-advertising started");
        }
        return 0;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        ESP_LOGI(TAG, "GAP adv complete (reason=%d)", event->adv_complete.reason);
        return 0;
    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(TAG, "GAP subscribe: conn=%d attr=%d notify=%d",
                 event->subscribe.conn_handle, event->subscribe.attr_handle,
                 event->subscribe.cur_notify);
        return 0;
    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "GAP mtu update: conn=%d mtu=%d",
                 event->mtu.conn_handle, event->mtu.value);
        return 0;

    /* ── 安全 / 绑定（配对持久化）──────────────────────────────────
     * 这三个事件是「首次配对成功后，重启自动重连、无需重配」的关键：
     *   ENC_CHANGE     — 链路已加密 = 绑定完成（LTK 已写入 NVS store）
     *   REPEAT_PAIRING — 中心端带着新密钥要重配，而我们已有旧 bond：
     *                    删旧 bond 并 RETRY，让配对能顺利完成
     *   PASSKEY_ACTION — Just Works（io_cap=NO_IO, mitm=0）下通常不触发；
     *                    为稳健仍处理 DISP/NUMCMP/INPUT 三种动作 */
    case BLE_GAP_EVENT_ENC_CHANGE:
        ESP_LOGI(TAG, "GAP enc change: status=%d conn=%d",
                 event->enc_change.status, event->enc_change.conn_handle);
        if (event->enc_change.status == 0)
            ESP_LOGI(TAG, "link encrypted (bond stored; auto-reconnect armed)");
        else
            ESP_LOGW(TAG, "encryption failed; central may retry pairing");
        return 0;

    case BLE_GAP_EVENT_REPEAT_PAIRING:
        {
            struct ble_gap_conn_desc d;
            /* 牺牲安全性换可用性：丢弃旧 bond，接受这次新配对。
             * 删完必须返回 RETRY，协议栈会校验 bond 确已删除后继续配对
             * （若未删净会再次上报本事件）。 */
            if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &d) == 0)
                ble_store_util_delete_peer(&d.peer_id_addr);
            ESP_LOGW(TAG, "repeat pairing: dropped old bond, accepting re-pair");
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;

    case BLE_GAP_EVENT_PASSKEY_ACTION:
        {
            struct ble_sm_io pkey = {0};
            pkey.action = event->passkey.params.action;
            if (event->passkey.params.action == BLE_SM_IOACT_DISP) {
                pkey.passkey = 123456;   /* 显示给中心端输入的静态 passkey */
                ble_sm_inject_io(event->passkey.conn_handle, &pkey);
            } else if (event->passkey.params.action == BLE_SM_IOACT_NUMCMP) {
                pkey.numcmp_accept = 1;  /* 数字比对：自动确认 */
                ble_sm_inject_io(event->passkey.conn_handle, &pkey);
            } else if (event->passkey.params.action == BLE_SM_IOACT_INPUT) {
                pkey.passkey = 123456;
                ble_sm_inject_io(event->passkey.conn_handle, &pkey);
            }
            ESP_LOGI(TAG, "PASSKEY_ACTION action=%d handled",
                     (int)event->passkey.params.action);
        }
        return 0;

    default:
        return 0;
    }
}

/* 设置广播报文内容（必须在使用广播数据前调用一次） */
static int ble_adv_set_fields(const char *device_name)
{
    static ble_uuid16_t s_hid_uuid;
    s_hid_uuid = (ble_uuid16_t)BLE_UUID16_INIT(QSHOU_GATT_SVC_HID_UUID);

    /* GAP 设备名：esp_hid 不会帮我们设（grep nimble_hidd.c 无
     * ble_svc_gap_device_name_set 调用），不设则 NimBLE 用默认值 "nimble"，
     * 中心端扫描时就显示成 "nimble"，无法辨识。必须显式设置。 */
    int nrc = ble_svc_gap_device_name_set(device_name);
    if (nrc != 0)
        ESP_LOGW(TAG, "ble_svc_gap_device_name_set failed; rc=%d", nrc);

    memset(&s_adv_fields, 0, sizeof s_adv_fields);

    /* 通用可发现 + 不支持 BR/EDR（本设备纯 BLE） */
    s_adv_fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;

    /* 🔴 根因修复（真机实测：鼠标可用、键盘/Consumer 全部无效）：
     * Android 的 HID host 依据**广播 appearance** 决定创建哪种输入设备。
     * 此前声明 Mouse(0x03C2) → 安卓只建「鼠标」设备、只订阅 RID1 的
     * Report 特征；键盘(RID2)/Consumer(RID3) 特征无订阅者 → notify 被
     * 静默丢弃 → 所有按键/文本输入失效（但鼠标照常，与现象完全吻合）。
     * 改为 Generic HID(0x03C0) → 安卓改为**按描述符解析**创建全部输入
     * 设备（鼠标+键盘+Consumer），三个 Report 特征都被订阅，键盘/文本
     * 才能送达。
     * ⚠️ 本 IDF 无 COMBO 宏；0x03C3 在该 IDF 是 JOYSTICK(0x03C4=GAMEPAD)，
     *    不可用；Generic 是唯一正确的复合设备外观。
     * ⚠️ 改后安卓侧必须「取消配对 → 重新搜索配对」才能刷新设备类型。 */
    s_adv_fields.appearance = ESP_HID_APPEARANCE_GENERIC;
    s_adv_fields.appearance_is_present = 1;

    /* ⚠️ 广播包（legacy adv）上限 31 字节（BLE_HS_ADV_MAX_SZ=31）。当前占用：
     *   flags 3 + appearance 4 + name (2+15) + uuid16 4 = 28 字节
     * 因此**不**再上报 TX power（会 +3 到 31，顶格无余量；名字一旦加长
     * 就溢出 → ble_gap_adv_set_fields 返回错误 → 广播失败 → 设备搜不到）。
     * 若必须加字段，先把设备名改短，或改用 scan response 承载名字。 */

    /* 完整设备名（中心端列表里就显示这个） */
    s_adv_fields.name = (uint8_t *)device_name;
    s_adv_fields.name_len = (uint8_t)strlen(device_name);
    s_adv_fields.name_is_complete = 1;

    /* HID 服务 UUID —— 缺了这条，中心端不会把它当 HID 设备 */
    s_adv_fields.uuids16 = &s_hid_uuid;
    s_adv_fields.num_uuids16 = 1;
    s_adv_fields.uuids16_is_complete = 1;

    int rc = ble_gap_adv_set_fields(&s_adv_fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_adv_set_fields failed; rc=%d", rc);
        return -1;
    }
    return 0;
}

/* 启动广播（无限时长，便于联调） */
static int ble_adv_start(void)
{
    struct ble_gap_adv_params adv_params;
    memset(&adv_params, 0, sizeof adv_params);
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;   /* 可连接、未定向 */
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;   /* 通用可发现 */
    adv_params.itvl_min = BLE_GAP_ADV_ITVL_MS(30);  /* 官方推荐 30~50ms */
    adv_params.itvl_max = BLE_GAP_ADV_ITVL_MS(50);

    int rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                               &adv_params, ble_gap_event_cb, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_adv_start failed; rc=%d", rc);
        return -1;
    }
    ESP_LOGI(TAG, "advertising started (HID 0x1812, name=\"%s\")",
             ble_svc_gap_device_name());
    return 0;
}

int platform_esp32_ble_start(const char *name)
{
    if (s_dev) {
        ESP_LOGW(TAG, "BLE HID already started");
        return 0;
    }

    /* 第一步只让 controller + host 内存池就绪，**不启动 host task**。
     * 顺序不可调换：esp_hidd_dev_init() 必须在 host sync 之前，否则 HID GATT
     * 服务永远不会被注册（详见 bt_stack_pre_init() 上方的长注释）。 */
    if (bt_stack_pre_init() != 0)
        return -1;

    /* report map 长度运行期填充（const 数组无法用非常量初始化） */
    s_report_maps[0].len = (uint16_t)qshou_hid_report_map_len;

    /* 设备名落到 static：sync 回调里要用，而 cfg 是栈上的局部变量。
     * 默认名用中性产品名 "Qshou"（复合 HID 设备，含鼠标+键盘+
     * Consumer）；不同于旧版 "Bluetooth Mouse"，更准确反映设备能力。
     * 设备类型由 appearance(GENERIC) + 描述符决定，与名字无关。 */
    /* 设备名 / VID / PID：优先 NVS qshou_cfg（冷生效），缺省用编译期值。
     * 注意：host 端已配对并缓存了旧名，改名后需在 host 端「取消配对」重连才生效。 */
    const char *use_name = (name && name[0]) ? name : "Qshou";
    char s_ble_name_cfg[32] = {0};
    if (platform_cfg_get_str("ble_name", s_ble_name_cfg, sizeof(s_ble_name_cfg)) && s_ble_name_cfg[0])
        use_name = s_ble_name_cfg;
    snprintf(s_ble_name, sizeof s_ble_name, "%s", use_name);

    uint16_t cfg_vid = QSHOU_HID_VID, cfg_pid = QSHOU_HID_PID;
    uint16_t w = 0;
    if (platform_cfg_get_u16("ble_vid", &w) && w) cfg_vid = w;
    if (platform_cfg_get_u16("ble_pid", &w) && w) cfg_pid = w;

    esp_hid_device_config_t cfg = {
        .vendor_id         = cfg_vid,
        .product_id        = cfg_pid,
        .version           = QSHOU_HID_VERSION,
        .device_name       = s_ble_name,
        .manufacturer_name = QSHOU_HID_MANUF,
        .serial_number     = QSHOU_HID_SERIAL,
        .report_maps       = s_report_maps,
        .report_maps_len   = (uint8_t)(sizeof(s_report_maps) / sizeof(s_report_maps[0])),
    };

    esp_err_t err = esp_hidd_dev_init(&cfg, ESP_HID_TRANSPORT_BLE,
                                      hid_event_handler, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_hidd_dev_init failed: %s", esp_err_to_name(err));
        s_dev = NULL;
        return -1;
    }
    ESP_LOGI(TAG, "BLE HID ready (Relative Mouse, report_map=%u bytes)",
             (unsigned)qshou_hid_report_map_len);

    /* 🔴 关键补漏：esp_hid 只注册 GATT，不负责广播（grep nimble_hidd.c 无
     * ble_gap_adv_* 调用）。设置广播字段 + 启动广播都已在
     * qshou_host_synced_cb() 里完成，此处不再调用
     * —— 两者都要求 host 已 enable（否则分别返回 BLE_HS_EDISABLED=30）。 */

    /* ── BLE 安全 / 绑定配置（配对持久化的关键，照搬官方示例 NimBLE 分支）
     * 必须在 nimble_port_run()（host sync）之前设置。缺了这段，设备只做
     * 「临时连接」而不 bonding → 安卓侧无法固化"已配对"条目 → 每次开机
     * 都要重新搜索手动配对（真机实测 2026-10-01 的根因之一）。
     *   sm_bonding = 1   → 交换并保存长期密钥（LTK/IRK）
     *   sm_sc      = 1   → 安全连接（与 sdkconfig 的 SM_SC=y 一致）
     *   sm_mitm    = 0   → 不强制 MITM：相对鼠标走 "Just Works" 无感配对，
     *                      不需要在手机上输入/确认 passkey
     *   sm_io_cap  = NO_IO → 无输入输出能力（与 mitm=0 组合即 Just Works；
     *                      注意 NO_IO+mitm=1 是矛盾组合，会导致配对失败）
     *   key dist   = ID|ENC → 交换身份解析密钥与加密密钥，供长期绑定使用
     * 是否把 bond 落盘到 NVS 由 CONFIG_BT_NIMBLE_NVS_PERSIST 决定
     * （sdkconfig.defaults 已开），此处只负责"确实做 bonding"。 */
    ble_hs_cfg.sm_bonding      = 1;
    ble_hs_cfg.sm_sc           = 1;
    ble_hs_cfg.sm_mitm         = 0;
    ble_hs_cfg.sm_io_cap       = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ID | BLE_SM_PAIR_KEY_DIST_ENC;
    ble_hs_cfg.sm_their_key_dist |= BLE_SM_PAIR_KEY_DIST_ID | BLE_SM_PAIR_KEY_DIST_ENC;

    /* 挂 sync 回调链：esp_hidd_dev_init() 刚把 sync_cb 换成 nimble_host_synced()，
     * 我们保存在其后追加自己的一层（负责 sync 后启动广播）。必须在启动
     * host task **之前**完成，否则 sync 早就发生了，回调永远不会被调用。 */
    s_prev_sync_cb = ble_hs_cfg.sync_cb;
    ble_hs_cfg.sync_cb = qshou_host_synced_cb;

    /* 最后一步才启动 host task —— sync 时会执行 ble_gatts_start()，
     * 把上面 esp_hidd_dev_init() 追加的 HID 服务真正注册进 GATT 数据库。 */
    if (bt_stack_start_host_task() != 0)
        return -1;

    return 0;
}

int platform_ble_emit_report(uint8_t report_id, const uint8_t *report, size_t len)
{
    if (!report || len == 0)
        return -1;

    /* 长度必须与 Report ID 在描述符里声明的字节数一致
     * （描述符↔线帧契约；不一致会被 host 误解析或触发 assert）。 */
    size_t want = (report_id == HID_REPORT_ID_MOUSE)    ? HID_REPORT_LEN
                : (report_id == HID_REPORT_ID_KEYBOARD) ? HID_KEYBOARD_REPORT_LEN
                : (report_id == HID_REPORT_ID_CONSUMER) ? HID_CONSUMER_REPORT_LEN
                                                        : 0;
    if (want == 0 || len != want)
        return -1;

    /* 未启动：视为不可发送，但不致命（上层可能早于 BLE ready 调用） */
    if (!s_dev)
        return -1;

    /* 未连接：静默丢弃并返回 0。
     * 语义理由：tap/swipe/key 是一串 report，若在未连接时返回 -1，
     * 上层会判整条动作失败；但「中心端不在」属于可恢复的外部状态，
     * 不应让固件逻辑层产生错误语义。 */
    if (!s_connected)
        return 0;

    /* 哑中继：直接透传已打包的 report，不解析语义（R20）。
     *
     * ⚠️⚠️ 线帧 **不带** Report ID 前缀 —— 这是真机逐帧反推出来的结论，勿改回。
     * Report ID 只用于 (a) host 关联 Report Reference 描述符(0x2908)、
     * (b) esp_hid 内部 find_report() 查 notify 句柄。复合设备下每个 Report ID
     * 在 HOGP 中是**独立的 Report 特征**，由下面的 report_id 参数路由；
     * 若再在载荷里补 ID 字节，host 会整体错位一字节（光标乱跳 / 键值错乱）。
     *
     *   RID 1 鼠标 3 字节 [buttons][dX][dY]
     *   RID 2 键盘 8 字节 [modifier][reserved][k0..k5]
     *   RID 3 Consumer 2 字节 [usage_lo][usage_hi] */
    esp_err_t err = esp_hidd_dev_input_set(s_dev, QSHOU_HID_MAP_INDEX,
                                           report_id,
                                           (uint8_t *)report, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "input_set(id=%u) failed: %s",
                 (unsigned)report_id, esp_err_to_name(err));
        return -1;
    }
    /* 记录发射时刻：推流侧的自动节流（D 方案）靠它判断「HID 活动窗口」 */
    s_last_hid_tx_ms = (uint32_t)(esp_timer_get_time() / 1000);
    return 0;
}

int platform_ble_emit(const uint8_t *report, size_t len)
{
    /* 鼠标专用包装：手势回放等既有路径全走这条（保持行为不变）。 */
    if (!hid_report_valid(report, len))
        return -1;
    return platform_ble_emit_report(HID_REPORT_ID_MOUSE, report, len);
}

/* ── HID 发射出口注入（见 platform.h 的 platform_hid_set_emit 说明）──────
 * 平台层的手势/按键队列跑在常驻任务里，不能搬到纯逻辑层；但它们必须与
 * HTTP 端点走同一条路由，否则 USB 后端接入后手势仍只走 BLE。
 * 这里只持有一个函数指针，不依赖 hid_router（保持平台层不反向依赖逻辑层）。 */
static platform_hid_emit_fn s_hid_emit = NULL;

void platform_hid_set_emit(platform_hid_emit_fn fn)
{
    s_hid_emit = fn;
}

/* 内部发射统一入口：有注入走注入（→ hid_router），否则直发本平台 BLE。 */
static int hid_emit_out(uint8_t report_id, const uint8_t *data, size_t len)
{
    if (s_hid_emit)
        return s_hid_emit(report_id, data, len);
    return platform_ble_emit_report(report_id, data, len);
}

/* ── 设备端轨迹回放（绕开 host 注入的沙箱限速）──────────────────────
 * 一次 /api/v1/gesture 触发 → 本任务以原生 BLE 速度连发一串 report，
 * host 看到的是连贯「按下→拖动→抬起」，而非被限速掐成的离散点。
 *
 * ⚠️⚠️ 必须串行化 —— 这是真机 bug 反推出来的硬约束，勿改回「每次 xTaskCreate」。
 *
 * 故障现象：先点四向滑动（dir），紧接着播 box 手势，方框不再闭合，
 *   终点稳定偏向「上一次 dir 的方向」恰好一格（= 单帧位移 120/12 = 10px）。
 *
 * 根因：旧实现每次触发都 xTaskCreate 一个新任务，零并发保护。
 *   上一个 dir 任务的尾帧 delay 未走完、notify 还压在 BLE 栈队列里，
 *   box 任务就已并发发帧 → dir 的 +10px 帧插进 box 序列 → box 净位移不再为 0。
 *   「偏移量恰好等于 dir 单帧位移」是这个机制的指纹（随机丢帧不会这么稳定）。
 *
 * 正解：单实例常驻任务 + 队列。play_gesture 只入队、绝不新建任务，
 *   任务内严格「取一个 → 播完 → 再取下一个」，天然无并发帧交错。 */
#define GEST_QUEUE_LEN 4
static QueueHandle_t s_gest_q = NULL;
static TaskHandle_t  s_gest_task = NULL;

/* 队列元素：手势几何 **或** 一次按键（按下→抬起）。
 * 二者共用同一串行队列 —— 这是硬约束：按键帧若插进正在回放的手势序列，
 * 会破坏 box/circle 的净位移闭合（与「先 dir 后 box 回不到原点」同源）。
 * 串行化的意义即「任何时刻只有一个作业在往 BLE 栈投帧」。 */
typedef struct {
    int            is_key;                        /* 0 = 手势几何, 1 = 按键 */
    gesture_spec_t spec;                          /* is_key == 0 */
    uint8_t        key_rid;                       /* is_key == 1：2=键盘 / 3=Consumer */
    uint8_t        key_len;                       /* 按键帧字节数（8 / 2） */
    uint8_t        key_press[HID_KEY_FRAME_MAX];  /* 按下帧 */
    uint8_t        key_release[HID_KEY_FRAME_MAX];/* 抬起帧 */
    uint16_t       key_hold_ms;                   /* 按下→抬起间隔 */
} hid_job_t;

static void gesture_task(void *arg);   /* 常驻回放任务（定义在后） */

/* 懒创建队列与常驻任务 —— 全生命周期只创建一次。
 * 旧 bug 是「每次触发都 xTaskCreate」，零并发保护 → 上一段手势的尾帧插进
 * 下一段（box 净位移不再为 0，终点稳定偏上次方向一格）。必须保持单实例。 */
static int hid_queue_ensure(void)
{
    if (!s_gest_q) {
        s_gest_q = xQueueCreate(GEST_QUEUE_LEN, sizeof(hid_job_t));
        if (!s_gest_q)
            return -1;
    }
    if (!s_gest_task) {
        if (xTaskCreate(gesture_task, "gesture", 4096, NULL, 5, &s_gest_task)
            != pdPASS)
            return -1;
    }
    return 0;
}

/* 相对模式下无需虚拟光标：每帧 dx/dy 是相对上一帧的位移，host 自行累加。 */

/* 上一次手势的可观测量：供 health 端点回读，用于区分
 * 「固件几何算错」与「host 侧丢帧/渲染问题」。 */
static int s_gest_frames = 0;
static int s_gest_dx = 0;
static int s_gest_dy = 0;

/* 帧节奏统计（ms）：相邻两帧「发出时刻」的实际间隔。
 * 理想 = step_ms 恒定；min/max 偏离即点距不均的直接证据。
 *   - 固件侧（tick 量化 / 被抢占）→ 应仅 ±1ms（FREERTOS_HZ=1000）
 *   - 若 min/max 大幅偏离 → notify 被 BLE 连接间隔量化（host 侧节奏）
 * 这样「点距不一致」就能被定位到固件调度 还是 BLE 连接参数。 */
static int s_gest_jit_min = 0;
static int s_gest_jit_max = 0;
static int s_gest_jit_avg = 0;

/* 发送成功/丢帧计数。
 * ⚠️ 丢帧是「点距不一致」的最直接原因：某一帧没发出去，相邻两点间距就变成
 * 2 倍（10px → 20px）。所以必须统计，且 **净位移只能累加真正发出去的帧**，
 * 否则 health 会「声称净位移 0 但屏幕上没回到原点」（统计与实际不符）。 */
static int s_gest_sent = 0;
static int s_gest_drop = 0;

static void gesture_task(void *arg)
{
    (void)arg;
    hid_job_t job;

    for (;;) {
        if (xQueueReceive(s_gest_q, &job, portMAX_DELAY) != pdTRUE)
            continue;

        /* ── 按键作业：按下 → 保持 → 抬起，仍在本任务的串行上下文内 ──
         * 与手势共用队列的意义就在这里：按键帧绝不会插进正在回放的手势序列。 */
        if (job.is_key) {
            hid_emit_out(job.key_rid, job.key_press,   job.key_len);
            vTaskDelay(pdMS_TO_TICKS(job.key_hold_ms ? job.key_hold_ms : 30));
            hid_emit_out(job.key_rid, job.key_release, job.key_len);
            vTaskDelay(pdMS_TO_TICKS(120));   /* settle：与下一个作业隔离 */
            ESP_LOGI(TAG, "key sent: rid=%u len=%u",
                     (unsigned)job.key_rid, (unsigned)job.key_len);
            continue;
        }

        const gesture_spec_t spec = job.spec;
        gesture_pt_t pts[256];
        int n = ble_hid_build_gesture_points(&spec, pts, 256);
        if (n <= 0) {
            ESP_LOGW(TAG, "gesture skipped: bad spec");
            continue;
        }
        ESP_LOGI(TAG, "gesture start: type=%d frames=%d step_ms=%d",
                 spec.type, n, spec.step_ms);

        /* ── 帧节奏：用 vTaskDelayUntil 做「绝对周期」而非 vTaskDelay 的
         *    「相对延迟」。相对延迟会把每帧的 emit 耗时（input_set 可能阻塞、
         *    等待 mbuf/BLE 资源）累加进周期 → 抖动随帧数漂移。
         *    绝对周期把下一帧的唤醒点钉在 last + period，emit 耗时被自动补偿，
         *    漂移不累积。这是「点距一致性」的固件侧保证。 */
        TickType_t period = pdMS_TO_TICKS(spec.step_ms);
        if (period == 0) period = 1;
        TickType_t last = xTaskGetTickCount();

        int net_x = 0, net_y = 0;
        int sent = 0, drop = 0;
        int jmin = 0, jmax = 0;
        int64_t jsum = 0; int jcnt = 0;
        int64_t t_prev = 0;

        for (int i = 0; i < n; i++) {
            /* 帧节奏采样：本帧「发出时刻」与上一帧的差 */
            int64_t t_now = esp_timer_get_time();
            if (i > 0 && t_prev > 0) {
                int d = (int)((t_now - t_prev) / 1000);
                if (jcnt == 0 || d < jmin) jmin = d;
                if (jcnt == 0 || d > jmax) jmax = d;
                jsum += d; jcnt++;
            }
            t_prev = t_now;

            /* 相对模式：本帧 dx/dy 即「光标相对上一帧的位移」，原样打包。
             * host 累加位移 → 天然无漂移；净位移 = 各帧增量之和
             * （box/circle 恒为 0 → 精确回到起点）。无需虚拟光标。 */
            uint8_t rep[3];
            hid_pack_report(rep, pts[i].buttons, pts[i].dx, pts[i].dy);

            /* 至多重试 3 次：BLE 栈 ACL 队列瞬时满时 input_set 会失败，
             * 丢一帧就会让该处点距翻倍、总位移永久偏掉。重试不打乱节奏
             * —— 下面的 vTaskDelayUntil 是绝对周期，会自动补偿这段耗时。
             * 注意：未连接时 emit 恒返回 0（静默丢弃），不会触发重试。 */
            int rc = -1;
            for (int a = 0; a < 3 && rc != 0; a++)
                rc = hid_emit_out(HID_REPORT_ID_MOUSE, rep, 3);

            if (rc == 0) {
                /* 只累加真正发出去的帧 → 统计口径与实际空中位移一致 */
                net_x += pts[i].dx;
                net_y += pts[i].dy;
                sent++;
            } else {
                drop++;
            }

            vTaskDelayUntil(&last, period);
        }

        s_gest_frames = n;
        s_gest_dx = net_x;
        s_gest_dy = net_y;
        s_gest_sent = sent;
        s_gest_drop = drop;
        s_gest_jit_min = jcnt ? jmin : 0;
        s_gest_jit_max = jcnt ? jmax : 0;
        s_gest_jit_avg = jcnt ? (int)(jsum / jcnt) : 0;
        ESP_LOGI(TAG, "gesture done: frames=%d net_dx=%d net_dy=%d "
                      "sent=%d drop=%d jit(min/avg/max)=%d/%d/%d ms (target=%d)",
                 n, net_x, net_y, sent, drop,
                 s_gest_jit_min, s_gest_jit_avg, s_gest_jit_max,
                 spec.step_ms);

        /* 与下一次手势之间留 settle 间隔：让可能积压在 BLE 栈里的 notify
         * 真正刷出去，避免跨手势的尾部帧溢出到下一个手势的开头。 */
        vTaskDelay(pdMS_TO_TICKS(150));
    }
}

int ble_hid_play_gesture(const gesture_spec_t *spec)
{
    if (!spec)
        return -1;

    if (hid_queue_ensure() != 0)
        return -1;

    hid_job_t job;
    memset(&job, 0, sizeof job);
    job.is_key = 0;
    job.spec   = *spec;

    /* 只入队，不新建任务。队列满 = 上一串还没播完 → 明确拒绝（HTTP 503），
     * 绝不并发播放。 */
    if (xQueueSend(s_gest_q, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "gesture busy: queue full");
        return 1;   /* 1 = 忙，由路由层映射成 503 */
    }
    return 0;
}

int ble_hid_send_key(const char *name)
{
    if (!name || !name[0])
        return -1;

    uint8_t rid = 0, len = 0;
    uint8_t press[HID_KEY_FRAME_MAX], release[HID_KEY_FRAME_MAX];
    if (ble_hid_key_frames(name, &rid, &len, press, release) != 0)
        return -1;   /* 名称未知 → 由路由层映射成 400 */

    if (hid_queue_ensure() != 0)
        return -1;

    hid_job_t job;
    memset(&job, 0, sizeof job);
    job.is_key      = 1;
    job.key_rid     = rid;
    job.key_len     = len;
    job.key_hold_ms = 30;
    memcpy(job.key_press,   press,   len);
    memcpy(job.key_release, release, len);

    if (xQueueSend(s_gest_q, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "key busy: queue full");
        return 1;   /* 1 = 忙，路由层映射成 503 */
    }
    return 0;
}

void platform_ble_last_gesture(int *frames, int *dx, int *dy)
{
    if (frames) *frames = s_gest_frames;
    if (dx)     *dx     = s_gest_dx;
    if (dy)     *dy     = s_gest_dy;
}

void platform_ble_gesture_jitter(int *min_ms, int *avg_ms, int *max_ms)
{
    if (min_ms) *min_ms = s_gest_jit_min;
    if (avg_ms) *avg_ms = s_gest_jit_avg;
    if (max_ms) *max_ms = s_gest_jit_max;
}

void platform_ble_gesture_sent(int *sent, int *drop)
{
    if (sent) *sent = s_gest_sent;
    if (drop) *drop = s_gest_drop;
}

/* 供 platform_health 查询 BLE 连接态（可选能力，弱符号避免耦合） */
bool platform_ble_is_connected(void)
{
    return s_connected;
}

/* ---- 控制 API：BLE 外设态管理（P3 真实实现）----
 *
 * 广播控制的语义模型（关键）：
 *   NimBLE 在「连接建立 / 断开」时会自动停/启广播，模块的 ble_gap_event_cb
 *   在 DISCONNECT 里也会自动重启广播。若用户想「保持关闭广播」，只调
 *   ble_gap_adv_stop() 是不够的 —— 下一次断连事件又会被自动重启。
 *   故用 s_adv_ctrl（ADV_AUTO / ADV_OFF）记录「广播由谁驱动」：
 *     - /ble/advertise {"on":1} → ADV_AUTO + 立即启动（回到自动模式）
 *     - /ble/advertise {"on":0} → ADV_OFF + 立即停止（此后断连不再自动重启）
 *   is_advertising() 上报「当前是否在广播」，用 ble_gap_adv_is_active
 *   不可用（本版本无此API）→ 以 s_adv_ctrl != ADV_OFF 近似上报，
 *   并叠加连接中（连接时协议栈自动停广播）这一事实。
 */
int platform_ble_advertise(bool on)
{
    if (!s_dev) {
        ESP_LOGW(TAG, "advertise: BLE not started");
        return -1;
    }
    if (on) {
        if (s_connected) {
            /* 已连接：连接期间无法广播（协议栈限制）。切回自动模式，
             * 断连后由事件回调自动重启广播。 */
            s_adv_ctrl = ADV_AUTO;
            ESP_LOGI(TAG, "advertise on: connected, will adv on disconnect");
            return 0;
        }
        s_adv_ctrl = ADV_AUTO;
        int rc = ble_adv_start();
        return (rc == 0) ? 0 : -1;
    }
    /* on == false：关闭广播，并禁止断开后自动重启 */
    s_adv_ctrl = ADV_OFF;
    int rc = ble_gap_adv_stop();
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGW(TAG, "adv_stop failed; rc=%d", rc);
        return -1;
    }
    ESP_LOGI(TAG, "advertising stopped (user)");
    return 0;
}

int platform_ble_disconnect(void)
{
    if (!s_connected) {
        ESP_LOGW(TAG, "disconnect: not connected");
        return -1;
    }
    /* 主动断连：用 GAP 事件捕获的 handle。reason 用「远端用户终止连接」
     * (0x13)，中心端看到的是常规断开，不会误判为链路故障。 */
    int rc = ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    if (rc != 0) {
        ESP_LOGW(TAG, "ble_gap_terminate failed; rc=%d", rc);
        return -1;
    }
    ESP_LOGI(TAG, "disconnect requested (handle=%u)", s_conn_handle);
    return 0;
}

int platform_ble_list_bonds(char *buf, size_t n)
{
    if (!buf || n == 0) return -1;
    /* 读全部已绑定 peer（NimBLE 公开 util，走真实 store，与协议栈一致）。
     * 每项输出 {"id":<索引>,"addr":"XX:XX:XX:XX:XX:XX","type":"public|random"}。
     * addr 取前 6 字节（省略 type 字节）大写 hex，与 WiFi bssid 展示一致。 */
    ble_addr_t peers[QSHOU_BOND_MAX];
    int num = 0;
    int rc = ble_store_util_bonded_peers(peers, &num, QSHOU_BOND_MAX);
    if (rc != 0) {
        ESP_LOGW(TAG, "bonded_peers failed; rc=%d", rc);
        snprintf(buf, n, "[]");
        return -1;
    }

    size_t off = 0;
    off += (size_t)snprintf(buf + off, (off < n) ? n - off : 0, "[");
    for (int i = 0; i < num && (off + 1) < n; i++) {
        /* 保持每个元素紧凑，字段间无空格 */
        off += (size_t)snprintf(buf + off, (off < n) ? n - off : 0,
                                "%s{\"id\":%d,\"addr\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"type\":\"%s\"}",
                                (i > 0) ? "," : "", i,
                                peers[i].val[5], peers[i].val[4], peers[i].val[3],
                                peers[i].val[2], peers[i].val[1], peers[i].val[0],
                                peers[i].type == BLE_OWN_ADDR_PUBLIC ? "public" : "random");
    }
    off += (size_t)snprintf(buf + off, (off < n) ? n - off : 0, "]");
    return 0;
}

int platform_ble_forget_bond(int id)
{
    ble_addr_t peers[QSHOU_BOND_MAX];
    int num = 0;
    int rc = ble_store_util_bonded_peers(peers, &num, QSHOU_BOND_MAX);
    if (rc != 0) {
        ESP_LOGW(TAG, "bonded_peers failed; rc=%d", rc);
        return -1;
    }
    if (id < 0 || id >= num) {
        ESP_LOGW(TAG, "forget_bond: id %d out of range (n=%d)", id, num);
        return -1;
    }
    rc = ble_store_util_delete_peer(&peers[id]);
    if (rc != 0) {
        ESP_LOGW(TAG, "delete_peer failed; rc=%d", rc);
        return -1;
    }
    ESP_LOGI(TAG, "forgot bond id=%d", id);
    return 0;
}

bool platform_ble_is_advertising(void)
{
    /* 连接中：协议栈自动停广播（即便 s_adv_ctrl 是 AUTO）。
     * 未连接时以驱动源判定：ADV_OFF → 未广播；ADV_AUTO → 已广播
     * （boot 由 host_synced 启动、断开由事件回调重启）。 */
    if (s_connected) return false;
    return s_adv_ctrl != ADV_OFF;
}

/* 推流侧自动节流（D 方案）读取：最近一次 HID 发射时刻（ms）。
 * 返回 0 表示从未发射过（boot 初期不触发节流）。 */
uint32_t platform_ble_last_tx_ms(void)
{
    return s_last_hid_tx_ms;
}

#endif /* PLATFORM_ESP32 */
