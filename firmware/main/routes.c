/*
 * main/routes.c — 路由实现
 *
 * 关键边界：固件是“哑中继”——直接把控制台算好的 5 字节 report 透传发射，
 * 自身不解析指令语义（满足 R20 密文约束）。SCR 坐标在 JSON 里仅作冗余记录。
 */
#include "main/routes.h"
#include "main/http_api.h"
#include "ble_hid/ble_hid.h"
#include "ble_hid/hid_report.h"
#include "hid_router/hid_router.h"
#include "platform/platform.h"
#include "platform/platform_camera.h"
#include "platform/platform_sysinfo.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

static uint32_t s_last_frame_ms = 0;

void routes_mark_frame(void)
{
    s_last_frame_ms = platform_uptime_ms();
}

/* ---- 位移预算（GESTURE_DIR / swipe 专用）------------------------------------
 * BLE 鼠标线帧 X/Y 是 **8-bit 有符号**，单帧位移上限 ±127。一条 DIR 手势最终能
 * 走多远 = MIN(帧数 × 127, 请求位移)，一旦请求超出预算就会被 hid_clamp_delta
 * 悄悄砍掉，而调用方读 HTTP 回包只看到自己请求的原值 —— **静默少走**。
 *
 * 2026-10-01 真机实测（hips 端 /api/v1/gesture health 对账）：
 *   请求 {"type":"dir","dx":2000,"steps":12}  →  回包 {"ok":true,"dx":2000}
 *   实际 health gest_dx = **1524** = 12×127，缩水 476 counts（23.8%）。
 * 更严重的是 /api/v1/swipe 原先写死 steps=16 → 上限仅 2032 counts，而控制器在
 * [0,10000] 的虚拟 HID 空间里算增量：一次全屏移动只交付 20%，**且控制器的
 * CursorState 仍照满额更新** → 光标信念与宿主真实位置永久失同步。
 *
 * 修复原则（放在固件，因为 ±127 的知识在固件）：
 *   ① 帧数按请求位移自适应展开 ceil(max(|dx|,|dy|) / 127)，保证走得满；
 *   ② 只扩不缩 —— 已验证过的小位移（box/UI 短距离）帧数不变，行为前后一致；
 *   ③ 帧数变多会拉长总时长，故同步下调 step_ms 使总耗时≈恒定（下限 15ms，
 *      等于 BLE 连接间隔；再快只会撞 ACL 队列造成 gest_drop）；
 *   ④ 真超出硬件上限（帧缓冲/节目预算）时，回包显式带 shortfall 字段，永不静默。 */
#define GEST_DIR_PER_FRAME_MAX  127    /* 单帧相对位移上限（8-bit 有符号） */
#define GEST_DIR_MIN_STEPS      4      /* 至少 4 帧：太稀疏会被 host 当成跳变 */
#define GEST_DIR_MAX_STEPS      200    /* 帧缓冲 pts[256] 的安全上限 */
#define GEST_DIR_TARGET_MS      1200   /* 手势总时长目标上限（ms） */
#define GEST_DIR_MIN_STEP_MS    15     /* = BLE 连接间隔，物理最小时隙 */
#define GEST_DIR_MAX_TOTAL      (GEST_DIR_MAX_STEPS * GEST_DIR_PER_FRAME_MAX)

/* 按「需要的 ⇒ 至少几帧」展开；want_steps 更大则尊重调用方（只扩不缩）。 */
static int gesture_dir_steps(int want_steps, int dx, int dy)
{
    int ax = dx < 0 ? -dx : dx;
    int ay = dy < 0 ? -dy : dy;
    int need = ax > ay ? ax : ay;
    int req  = (need + GEST_DIR_PER_FRAME_MAX - 1) / GEST_DIR_PER_FRAME_MAX;
    if (req < GEST_DIR_MIN_STEPS) req = GEST_DIR_MIN_STEPS;
    if (want_steps > req)         req = want_steps;
    if (req > GEST_DIR_MAX_STEPS) req = GEST_DIR_MAX_STEPS;
    return req;
}

/* 锁定总时长：帧数翻倍则间隔减半，但不低于 15ms（再快只会丢帧）。 */
static int gesture_dir_step_ms(int want_ms, int steps)
{
    int ms = (want_ms > 0) ? want_ms : 90;
    if (steps > 0 && ms * steps > GEST_DIR_TARGET_MS) {
        int bounded = GEST_DIR_TARGET_MS / steps;
        if (bounded < GEST_DIR_MIN_STEP_MS) bounded = GEST_DIR_MIN_STEP_MS;
        if (bounded < ms) ms = bounded;
    }
    return ms;
}

static int handle_health(char *resp, size_t cap)
{
    int gf = 0, gx = 0, gy = 0;
    int jmin = 0, javg = 0, jmax = 0;
    int gsent = 0, gdrop = 0;
    platform_ble_last_gesture(&gf, &gx, &gy);
    platform_ble_gesture_jitter(&jmin, &javg, &jmax);
    platform_ble_gesture_sent(&gsent, &gdrop);
    /* IO2 灯诊断：LEDC 是否可用 + 启动时 pad 自检电平（-1=未测）。
     * pad_high=1 & pad_low=0 = GPIO2 确实在控制 pad；两者同值 = pad 被外部拉死。
     * pad_now/pad_pct = LED 任务每 tick 实测：pad 当前电平与高电平占比 0..100。
     * 拿 pad_pct 与设定 duty/255 对比，即可判断 PWM 是否真打到了 pad 上，
     * 不必靠肉眼分辨「很暗」与「完全不亮」。 */
    int io2_ready = 0, io2_ph = -1, io2_pl = -1, io2_pn = -1, io2_pc = -1;
    platform_led_io2_diag(&io2_ready, &io2_ph, &io2_pl, &io2_pn, &io2_pc);
    int n = snprintf(resp, cap,
        "{\"uptime_s\":%lu,\"free_heap\":%lu,\"reset_reason\":\"%s\","
        "\"last_frame_ms\":%lu,\"wifi_rssi\":%d,\"ble_connected\":%d,"
        "\"gest_frames\":%d,\"gest_dx\":%d,\"gest_dy\":%d,"
        "\"gest_jit_min\":%d,\"gest_jit_avg\":%d,\"gest_jit_max\":%d,"
        "\"gest_sent\":%d,\"gest_drop\":%d,"
        "\"io2_ledc\":%d,\"io2_pad_high\":%d,\"io2_pad_low\":%d,"
        "\"io2_pad_now\":%d,\"io2_pad_pct\":%d,\"io2_gpio_mode\":%d}",
        (unsigned long)(platform_uptime_ms() / 1000u),
        (unsigned long)platform_free_heap(),
        platform_reset_reason(),
        (unsigned long)s_last_frame_ms,
        platform_wifi_rssi(),
        platform_ble_is_connected() ? 1 : 0,
        gf, gx, gy, jmin, javg, jmax, gsent, gdrop,
        io2_ready, io2_ph, io2_pl, io2_pn, io2_pc,
        (int)platform_led_io2_mode_get());
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* GET /api/v1/sysinfo —— 设备信息面板（存储 / 内存 / 任务栈水位）
 *
 * 与 /api/v1/health 的分工：health 是「手势与 IO2 诊断遥测」，本端点是
 * 「资源水位」——回答"设备还剩多少存储/内存、哪个任务快爆栈"。
 *
 * 为什么没有 CPU 负载百分比：IDF v6.1 已移除 esp_system_get_cpu_usage()，
 * 要拿 idle 占比必须开 CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS（改 sdkconfig
 * = 全量重编 ~44min）。免重编的替代是「任务栈剩余水位」，对排查 HID 抖动更实用。
 *
 * 🔴 registry 事实（勿凭记忆改，字段名对不上 = 真机编译失败）：
 *    - NVS 走条目口径（nvs_stats_t 无 page 字段），字节数按 条目×32 估算；
 *    - 任务只报「历史最小剩余栈」，不报「栈容量」—— 本固件未开
 *      configUSE_TRACE_FACILITY，没有公开 API 能拿到栈容量，宁缺勿谎报。
 *
 * 🔴 storage(1MB) 分区从未被挂载（固件无 esp_vfs_spiffs_init/littlefs_init），
 *    故这里只报分区表几何信息 + mounted=0 标记，
 *    **不谎报文件系统占用**。 */
#define SYSINFO_MAX_PARTS  8
#define SYSINFO_MAX_TASKS  8

static int handle_sysinfo(char *resp, size_t cap)
{
    qshou_nvs_info nvs;
    qshou_mem_info mem;
    qshou_part_info parts[SYSINFO_MAX_PARTS];
    qshou_task_info tasks[SYSINFO_MAX_TASKS];

    platform_sysinfo_nvs(&nvs);
    platform_sysinfo_mem(&mem);
    int nparts = platform_sysinfo_partitions(parts, SYSINFO_MAX_PARTS);
    if (nparts < 0) nparts = 0;
    int ntasks = platform_sysinfo_tasks(tasks, SYSINFO_MAX_TASKS);
    if (ntasks < 0) ntasks = 0;
    if (nparts > SYSINFO_MAX_PARTS) nparts = SYSINFO_MAX_PARTS;
    if (ntasks > SYSINFO_MAX_TASKS) ntasks = SYSINFO_MAX_TASKS;

    size_t used = 0;
    int n = snprintf(resp, cap,
        "{\"ok\":true,"
        "\"nvs\":{\"valid\":%d,\"part_size\":%lu,\"used_entries\":%lu,"
        "\"total_entries\":%lu,\"free_entries\":%lu,\"namespace_count\":%lu,"
        "\"used_bytes_est\":%lu,\"cap_bytes_est\":%lu},"
        "\"mem\":{\"heap_free\":%lu,\"heap_min_free\":%lu,\"heap_largest\":%lu,"
        "\"internal_total\":%lu,\"psram_total\":%lu,\"psram_free\":%lu,"
        "\"psram_largest\":%lu},"
        "\"storage_mounted\":0,"
        "\"entry_bytes\":32,"
        "\"partitions\":[",
        nvs.valid,
        (unsigned long)nvs.part_size,      (unsigned long)nvs.used_entries,
        (unsigned long)nvs.total_entries,  (unsigned long)nvs.free_entries,
        (unsigned long)nvs.namespace_count,
        (unsigned long)nvs.used_bytes_est, (unsigned long)nvs.cap_bytes_est,
        (unsigned long)mem.heap_free,     (unsigned long)mem.heap_min_free,
        (unsigned long)mem.heap_largest,  (unsigned long)mem.internal_total,
        (unsigned long)mem.psram_total,   (unsigned long)mem.psram_free,
        (unsigned long)mem.psram_largest);
    if (n < 0 || (size_t)n >= cap) return 500;
    used = (size_t)n;

    for (int i = 0; i < nparts; i++) {
        /* label 已由接缝层截断到 23 字节且保证 NUL 结尾；此处只做二次兜底，
         * 防止畸形 label 破坏 JSON 结构（否则整个响应会被前端丢弃）。 */
        char lab[24];
        size_t k = 0;
        for (; k < sizeof(lab) - 1 && parts[i].label[k]; k++) {
            char c = parts[i].label[k];
            if (c < 0x20 || c == '"' || c == '\\') c = '?';
            lab[k] = c;
        }
        lab[k] = '\0';
        n = snprintf(resp + used, cap - used,
            "%s{\"label\":\"%s\",\"offset\":%lu,\"size\":%lu,\"app\":%u,"
            "\"data\":%u,\"mounted\":%u}",
            i ? "," : "", lab,
            (unsigned long)parts[i].offset, (unsigned long)parts[i].size,
            (unsigned)parts[i].is_app, (unsigned)parts[i].is_data,
            (unsigned)parts[i].mounted);
        if (n < 0 || (size_t)n >= cap - used) return 500;
        used += (size_t)n;
    }

    n = snprintf(resp + used, cap - used, "],\"tasks\":[");
    if (n < 0 || (size_t)n >= cap - used) return 500;
    used += (size_t)n;

    /* 用独立计数写逗号：invalid 任务被跳过，不能拿循环下标当分隔依据。 */
    int wrote = 0;
    for (int i = 0; i < ntasks; i++) {
        if (!tasks[i].valid) continue;
        char tn[24];
        size_t k = 0;
        for (; k < sizeof(tn) - 1 && tasks[i].name[k]; k++) {
            char c = tasks[i].name[k];
            if (c < 0x20 || c == '"' || c == '\\') c = '?';
            tn[k] = c;
        }
        tn[k] = '\0';
        n = snprintf(resp + used, cap - used,
            "%s{\"name\":\"%s\",\"stack_free\":%lu}",
            wrote ? "," : "", tn,
            (unsigned long)tasks[i].stack_free);
        if (n < 0 || (size_t)n >= cap - used) return 500;
        used += (size_t)n;
        wrote++;
    }

    n = snprintf(resp + used, cap - used, "]}");
    if (n < 0 || (size_t)n >= cap - used) return 500;
    return 200;
}

/* GET /api/v1/led[?io2=<0..255|-1>] —— IO2 灯的现场测试/判极性端点。
 * 因 BLE 测试期间禁开串口，这是唯一能在不看日志的情况下处置灯的手段：
 *   io2=255 → 原始 duty 拉满 → 灯亮即「高有效」，暗即反之；
 *   io2=0   → 原始 duty 归零 → 灯亮即「低有效」；
 *   io2=-1  → 恢复自动（BLE 已连接才亮）；
 *   pol=1|0 → 运行时把极性翻成高/低有效（**判错不必重烧**），pol=-1 恢复编译期值。
 *   mode=1  → IO2 改 GPIO 直驱（彻底绕开 LEDC），mode=0 → 回到 PWM。
 *             同一亮度/极性下「GPIO 能亮、PWM 不亮」= LEDC 侧问题，与极性无关。
 * 返回结果里带 LEDC 状态与启动自检的 pad 电平，便于区分「极性错」还是「pad 被拉死」。
 * 🔴 注：2026-10-01 前 IO2 恒亮不起来，真因是 IO2 与相机 XCLK 抢同一组 LEDC
 *    timer/channel（已改 TIMER_1/CHANNEL_1）—— 若再遇「怎么翻极性都不亮」，
 *    先查本项目里还有谁在用 LEDC，而不是继续折磨极性。 */
static int handle_led(const char *path, char *resp, size_t cap)
{
    const char *q = strchr(path, '?');
    if (q) {
        const char *p = strstr(q + 1, "io2=");
        if (p)
            platform_led_set_io2(atoi(p + 4));
        p = strstr(q + 1, "pol=");
        if (p)
            platform_led_set_io2_pol(atoi(p + 4));
        p = strstr(q + 1, "mode=");
        if (p)
            platform_led_set_io2_mode(atoi(p + 5));
    }
    int ready = 0, ph = -1, pl = -1, pn = -1, pc = -1;
    platform_led_io2_diag(&ready, &ph, &pl, &pn, &pc);
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"io2_ledc\":%d,\"io2_pad_high\":%d,\"io2_pad_low\":%d,"
        "\"io2_pad_now\":%d,\"io2_pad_pct\":%d,\"io2_gpio_mode\":%d,"
        "\"hint\":\"default gpio mode: io2>0 -> forced on (polarity applied), "
        "io2=0 -> forced off, io2=-1 -> auto (BLE connected); "
        "mode=0 -> PWM where io2 is raw duty; pol=1|0|-1 -> flip polarity at runtime\"}",
        ready, ph, pl, pn, pc, (int)platform_led_io2_mode_get());
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_tap(const char *body, char *resp, size_t cap)
{
    tap_req_t req;
    if (http_parse_tap(body, &req) != 0)
        return 400;
    /* 相对鼠标下「点按」= 在当前光标处按下并抬起。用专门的 GESTURE_TAP：
     * 1px 净零抖动锚定按下，规避安卓丢弃「零位移 down→up」。走与 box 相同的
     * 已验证链路，保持串行化。 */
    gesture_spec_t spec;
    memset(&spec, 0, sizeof spec);
    spec.type    = GESTURE_TAP;
    spec.step_ms = 80;
    int rc = ble_hid_play_gesture(&spec);
    if (rc != 0)
        return (rc == 1) ? 503 : 400;
    int n = snprintf(resp, cap, "{\"ok\":true,\"note\":\"relative tap at current cursor\"}");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_swipe(const char *body, char *resp, size_t cap)
{
    swipe_req_t req;
    if (http_parse_swipe(body, &req) != 0)
        return 400;
    /* 相对鼠标下 swipe 是「相对位移」：把起点/终点 5 字节包（[state,Xh,Xl,Yh,Yl]）
     * 重新解释为带符号 16-bit 相对增量 (end - start)，构造 GESTURE_DIR 手势。
     * 旧控制器契约仍可工作，且走与 box 相同的已验证链路。 */
    int16_t sx = (int16_t)((req.hid_report_start[1] << 8) | (req.hid_report_start[2] & 0xFF));
    int16_t sy = (int16_t)((req.hid_report_start[3] << 8) | (req.hid_report_start[4] & 0xFF));
    int16_t ex = (int16_t)((req.hid_report_end[1]   << 8) | (req.hid_report_end[2]   & 0xFF));
    int16_t ey = (int16_t)((req.hid_report_end[3]   << 8) | (req.hid_report_end[4]   & 0xFF));
    int dx = (int)ex - (int)sx;
    int dy = (int)ey - (int)sy;
    if (dx >  GEST_DIR_MAX_TOTAL) dx =  GEST_DIR_MAX_TOTAL;
    if (dx < -GEST_DIR_MAX_TOTAL) dx = -GEST_DIR_MAX_TOTAL;
    if (dy >  GEST_DIR_MAX_TOTAL) dy =  GEST_DIR_MAX_TOTAL;
    if (dy < -GEST_DIR_MAX_TOTAL) dy = -GEST_DIR_MAX_TOTAL;

    /* 帧数按位移自适应展开 —— 旧实现写死 16 帧（上限 2032 counts），
     * 而控制器在 [0,10000] 空间算增量 → 大位移静默少走且控制器失同步。 */
    int steps   = gesture_dir_steps(16, dx, dy);
    int step_ms = gesture_dir_step_ms(60, steps);
    gesture_spec_t spec;
    memset(&spec, 0, sizeof spec);
    spec.type    = GESTURE_DIR;
    spec.steps   = steps;
    spec.step_ms = step_ms;
    spec.dx      = dx;
    spec.dy      = dy;
    spec.press   = 1;   /* 控制器 swipe = 按住拖拽（按下→移动→抬起） */
    int rc = ble_hid_play_gesture(&spec);
    if (rc != 0)
        return (rc == 1) ? 503 : 400;
    /* shortfall：预算确实覆盖不了（>200 帧）时明确告知，绝不假装成功 */
    int budget    = steps * GEST_DIR_PER_FRAME_MAX;
    int shortfall = ((dx < 0 ? -dx : dx) > budget || (dy < 0 ? -dy : dy) > budget) ? 1 : 0;
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"dx\":%d,\"dy\":%d,\"steps_eff\":%d,\"step_ms_eff\":%d,"
        "\"budget\":%d,\"shortfall\":%d}",
        dx, dy, steps, step_ms, budget, shortfall);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_gesture(const char *body, char *resp, size_t cap)
{
    char type[32];
    long steps = 12, step_ms = 90;
    long dx = 0, dy = 0, press = 0;
    if (!json_find_str(body, "type", type, sizeof(type)))
        strcpy(type, "box");
    json_find_int(body, "steps", &steps);
    json_find_int(body, "step_ms", &step_ms);
    json_find_int(body, "dx", &dx);
    json_find_int(body, "dy", &dy);
    json_find_int(body, "press", &press);
    if (steps < 1)   steps = 1;
    if (steps > 200) steps = 200;
    if (step_ms < 10)   step_ms = 10;
    if (step_ms > 1000) step_ms = 1000;
    /* UI 的「位移距离」即相对像素：每帧增量 = 总位移/steps（build 内夹到 ±127）。
     * 相对鼠标下直接作为相对位移下发，无需换算。
     * ⚠️ 上限从写死的 ±2000 改为「帧数 × 127」的真实位移预算，并让帧数自适应展开
     *    （见上方 gesture_dir_steps 注释）：原先 dx=2000 配默认 steps=12
     *    只能落地 1524 counts，回包却仍报 {"dx":2000}。 */
    if (dx >  GEST_DIR_MAX_TOTAL) dx =  GEST_DIR_MAX_TOTAL;
    if (dx < -GEST_DIR_MAX_TOTAL) dx = -GEST_DIR_MAX_TOTAL;
    if (dy >  GEST_DIR_MAX_TOTAL) dy =  GEST_DIR_MAX_TOTAL;
    if (dy < -GEST_DIR_MAX_TOTAL) dy = -GEST_DIR_MAX_TOTAL;
    int hid_dx = (int)dx;
    int hid_dy = (int)dy;

    /* DIR 的位移量取决于 帧数×127，必须按位移展开帧数；box/line/circle 是固定
     * 几何（单边 ~120px），steps 不参与位移量，保持调用方原值不动。 */
    int is_dir = (strcmp(type, "dir") == 0);
    if (is_dir) {
        steps   = gesture_dir_steps((int)steps, hid_dx, hid_dy);
        step_ms = gesture_dir_step_ms((int)step_ms, (int)steps);
    }

    gesture_spec_t spec;
    spec.type    = (strcmp(type, "line") == 0)   ? GESTURE_LINE
                 : (strcmp(type, "circle") == 0) ? GESTURE_CIRCLE
                 : (strcmp(type, "dir")   == 0)  ? GESTURE_DIR
                 : (strcmp(type, "tap")   == 0)  ? GESTURE_TAP
                 : GESTURE_BOX;
    spec.steps   = (int)steps;
    spec.step_ms = (int)step_ms;
    spec.dx      = hid_dx;   /* 相对位移，单位 = HID count（单帧上限 ±127） */
    spec.dy      = hid_dy;
    spec.press   = (press != 0) ? 1 : 0;   /* 0=纯移动(四向移动按钮)；非零=按住(点击/拖拽) */

    /* 设备端异步回放：立即返回，轨迹由 ESP32 以原生 BLE 速度播放。
     * 返回 1 = 上一串还没播完（队列满）→ 503 明确拒绝，绝不并发播放
     * （并发会让两段轨迹的帧交错，box 净位移不再是 0 → 回不到原点）。 */
    int rc = ble_hid_play_gesture(&spec);
    if (rc != 0)
        return (rc == 1) ? 503 : 400;
    /* 回包报**实际**采用的帧数/节奏，DIR 额外给预算与 shortfall ——
     * 过去正因为回包 echo 请求原值，才让「少走 476 counts」一直藏着。 */
    int budget    = (int)steps * GEST_DIR_PER_FRAME_MAX;
    int shortfall = is_dir && (((int)dx < 0 ? -(int)dx : (int)dx) > budget ||
                               ((int)dy < 0 ? -(int)dy : (int)dy) > budget) ? 1 : 0;
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"type\":\"%s\",\"steps\":%d,\"step_ms\":%d,"
        "\"dx\":%d,\"dy\":%d,\"budget\":%d,\"shortfall\":%d,"
        "\"note\":\"playing on device\"}",
        type, (int)steps, (int)step_ms, (int)dx, (int)dy, budget, shortfall);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_key(const char *body, char *resp, size_t cap)
{
    key_req_t req;
    if (http_parse_key(body, &req) != 0)
        return 400;
    /* 命名键（HOME/BACK/VOL_UP/VOL_DOWN/MUTE/PLAY_PAUSE/… 或 ENTER/ESC/方向键）
     * 或单个 ASCII 字符。映射到 键盘(Report ID 2) 或 Consumer Control(Report ID 3)，
     * 与手势**共用同一串行队列**（按键帧绝不插进正在回放的手势序列）。 */
    int rc = ble_hid_send_key(req.key);
    if (rc != 0)
        return (rc == 1) ? 503 : 400;   /* 1 = 忙（队列满）；<0 = 未知键名 */
    int n = snprintf(resp, cap, "{\"ok\":true,\"key\":\"%s\"}", req.key);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_calib_point(const char *body, char *resp, size_t cap)
{
    calib_point_req_t req;
    if (http_parse_calib_point(body, &req) != 0)
        return 400;
    /* 缓冲由控制台侧集中求解 H（v1.9.1 决策）；固件仅确认收到 */
    platform_log("CAL", "point %d cap=(%.1f,%.1f) buffered", req.index, req.cap_x, req.cap_y);
    int n = snprintf(resp, cap, "{\"ok\":true,\"index\":%d}", req.index);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

static int handle_calib_commit(const char *body, char *resp, size_t cap)
{
    calib_commit_req_t req;
    http_parse_calib_commit(body, &req); /* 始终接受 */
    platform_log("CAL", "commit device=%s", req.has_device ? req.device_id : "(none)");
    int n = snprintf(resp, cap, "{\"ok\":true}");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* POST /api/v1/camera —— 相机传感器参数（真实生效，非预览 CSS 滤镜）。
 * 字段（均可选，缺省沿用上次值）：
 *   exposure_auto : 1=自动曝光(AEC)；0=手动（用 exposure_value）
 *   exposure_value: 手动曝光值 0..1200（仅 auto=0 生效）
 *   auto_wb      : 1=自动白平衡(AWB)；0=关闭
 *   auto_gain    : 1=自动增益(AGC)；0=关闭
 *   brightness / contrast / saturation : -2..2（传感器 DSP）
 *   frame_size   : 分辨率枚举（0..13；本构建真值表：QVGA=6 / 480×320=9 / VGA(640×480)=10 / SVGA=11 / XGA=12 / 720p=13）。运行时切换、免重烧。
 * 返回当前实际生效值，便于 UI 回显。 */
static int handle_camera(const char *body, char *resp, size_t cap)
{
    long exposure_auto = -1, exposure_value = -1, brightness = -9,
         contrast = -9, saturation = -9, auto_wb = -1, auto_gain = -1,
         frame_size = -1;
    json_find_int(body, "exposure_auto", &exposure_auto);
    json_find_int(body, "exposure_value", &exposure_value);
    json_find_int(body, "auto_wb", &auto_wb);
    json_find_int(body, "auto_gain", &auto_gain);
    json_find_int(body, "brightness", &brightness);
    json_find_int(body, "contrast", &contrast);
    json_find_int(body, "saturation", &saturation);
    json_find_int(body, "frame_size", &frame_size);

    int cur_auto = 1, cur_aec = 0, cur_bri = 0, cur_con = 0, cur_sat = 0,
        cur_wb = 1, cur_gain = 1, cur_framesize = 10;
    platform_camera_get_settings(&cur_auto, &cur_aec, &cur_bri, &cur_con, &cur_sat,
                                &cur_wb, &cur_gain);
    platform_camera_get_framesize(&cur_framesize);

    int set_auto = (exposure_auto >= 0) ? (exposure_auto ? 1 : 0) : cur_auto;
    int set_aec  = (exposure_value >= 0) ? (int)exposure_value : cur_aec;
    int set_bri  = (brightness >= -2 && brightness <= 2) ? (int)brightness : cur_bri;
    int set_con  = (contrast   >= -2 && contrast   <= 2) ? (int)contrast   : cur_con;
    int set_sat  = (saturation >= -2 && saturation <= 2) ? (int)saturation : cur_sat;
    int set_wb   = (auto_wb   >= 0) ? (auto_wb   ? 1 : 0) : cur_wb;
    int set_gain = (auto_gain >= 0) ? (auto_gain ? 1 : 0) : cur_gain;

    platform_camera_set_exposure(set_auto, set_aec);
    platform_camera_set_auto_wb(set_wb);
    platform_camera_set_auto_gain(set_gain);
    platform_camera_set_image_adj(set_bri, set_con, set_sat);
    if (frame_size >= 0)
        platform_camera_set_framesize((int)frame_size);

    platform_camera_get_settings(&cur_auto, &cur_aec, &cur_bri, &cur_con, &cur_sat,
                                &cur_wb, &cur_gain);
    platform_camera_get_framesize(&cur_framesize);
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"exposure_auto\":%d,\"exposure_value\":%d,"
        "\"auto_wb\":%d,\"auto_gain\":%d,"
        "\"brightness\":%d,\"contrast\":%d,\"saturation\":%d,"
        "\"frame_size\":%d}",
        cur_auto, cur_aec, cur_wb, cur_gain, cur_bri, cur_con, cur_sat,
        cur_framesize);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- /api/v1/settings：设备配置（冷生效）---- */
static int settings_err(char *resp, size_t cap, const char *msg)
{
    int n = snprintf(resp, cap, "{\"ok\":false,\"error\":\"%s\"}", msg);
    return (n < 0 || (size_t)n >= cap) ? 500 : 400;
}

static int handle_settings(const char *body, char *resp, size_t cap)
{
    /* ---- GET：无 body 即预填（回显 NVS 当前值，缺省回退编译期默认）---- */
    if (body == NULL || body[0] == '\0') {
        char ble_name[32] = {0}, mdns_host[32] = {0}, wifi_ssid[64] = {0};
        uint8_t b = 0; uint16_t w = 0;
        int vid = 0x303A, pid = 0x0001, fs = 10, q = 16, max_uri = 16, resp_cap = 4096;
        platform_cfg_get_str("ble_name",  ble_name,  sizeof(ble_name));
        platform_cfg_get_u16("ble_vid",  &w); if (w) vid = w;
        platform_cfg_get_u16("ble_pid",  &w); if (w) pid = w;
        platform_cfg_get_str("mdns_host", mdns_host, sizeof(mdns_host));
        platform_cfg_get_u8("cam_framesize", &b); if (b) fs = b;
        platform_cfg_get_u8("cam_quality",   &b); if (b) q = b;
        platform_cfg_get_u8("http_max_uri",  &b); if (b) max_uri = b;
        platform_cfg_get_u16("http_resp_cap", &w); if (w) resp_cap = w;
        /* ⚠️ resp_cap 在 platform_net.c 里被当**下限**抬回编译期 RESP_CAP，
         * 这里回显的是 NVS 原始值；若它小于实际生效值，以实际为准。 */
        platform_cfg_get_str("wifi_ssid", wifi_ssid, sizeof(wifi_ssid));
        /* ⚠️ wifi_pass 不回显（安全）：前端留空表示不改密码。 */
        int n = snprintf(resp, cap,
            "{\"ok\":true,\"ble_name\":\"%s\",\"ble_vid\":%d,\"ble_pid\":%d,"
            "\"mdns_host\":\"%s\",\"cam_framesize\":%d,\"cam_quality\":%d,"
            "\"wifi_ssid\":\"%s\",\"wifi_pass\":\"\","
            "\"http_resp_cap\":%d,\"http_max_uri\":%d}",
            ble_name, vid, pid, mdns_host, fs, q, wifi_ssid, resp_cap, max_uri);
        return (n < 0 || (size_t)n >= cap) ? 500 : 200;
    }

    /* ---- POST：解析可选字段 → 校验/钳制 → 写 NVS → 重启冷生效 ---- */
    char ble_name[32]={0}, mdns_host[32]={0}, wifi_ssid[64]={0}, wifi_pass[64]={0};
    int has_ble  = json_find_str(body, "ble_name",  ble_name,  sizeof(ble_name));
    int has_mdns = json_find_str(body, "mdns_host", mdns_host, sizeof(mdns_host));
    int has_ssid = json_find_str(body, "wifi_ssid", wifi_ssid, sizeof(wifi_ssid));
    int has_pass = json_find_str(body, "wifi_pass", wifi_pass, sizeof(wifi_pass));
    long vid=0, pid=0, fs=0, q=0, max_uri=0, resp_cap=0;
    int has_vid = json_find_int(body, "ble_vid", &vid);
    int has_pid = json_find_int(body, "ble_pid", &pid);
    int has_fs  = json_find_int(body, "cam_framesize", &fs);
    int has_q   = json_find_int(body, "cam_quality", &q);
    int has_mu  = json_find_int(body, "http_max_uri", &max_uri);
    int has_rc  = json_find_int(body, "http_resp_cap", &resp_cap);

    if (has_ble && strlen(ble_name) > 31)   return settings_err(resp, cap, "ble_name too long (<=31)");
    if (has_mdns && strlen(mdns_host) > 31) return settings_err(resp, cap, "mdns_host too long (<=31)");
    if (has_vid && (vid < 0 || vid > 0xFFFF)) return settings_err(resp, cap, "ble_vid out of range");
    if (has_pid && (pid < 0 || pid > 0xFFFF)) return settings_err(resp, cap, "ble_pid out of range");
    /* 白名单 = 当前枚举真值表可选档位（6/9/10/11/12/13/15/16）。
     * 15=UXGA(1600×1200) OV2640/OV5640 均支持；16=FHD(1920×1080) 仅 OV5640
     * （OV2640 上 boot 时 esp_camera_init 会按 sensor 表夹到 UXGA 并告警，自愈不崩）。 */
    if (has_fs && !(fs==6||fs==9||fs==10||fs==11||fs==12||fs==13||fs==15||fs==16))
        return settings_err(resp, cap, "cam_framesize invalid (6/9/10/11/12/13/15/16)");
    if (has_q && (q < 0 || q > 63))    return settings_err(resp, cap, "cam_quality out of range (0..63)");
    if (has_mu && (max_uri < 13 || max_uri > 32)) return settings_err(resp, cap, "http_max_uri out of range (13..32)");
    if (has_rc && (resp_cap < 1024 || resp_cap > 8192)) return settings_err(resp, cap, "http_resp_cap out of range (1024..8192)");
    /* 🔴 http_resp_cap 允许写 1024，但 platform_net.c 把它当**下限**抬回
     * RESP_CAP(4096) —— 因为 /sysinfo 与 /schema 的最坏响应超过 2048，
     * 配小等于让这两个端点恒 500。这里只在 GET 回显上做提示，避免用户
     * 以为自己的配置生效了。 */

    if (has_ble)  platform_cfg_set_str("ble_name", ble_name);
    if (has_vid)  platform_cfg_set_u16("ble_vid", (uint16_t)vid);
    if (has_pid)  platform_cfg_set_u16("ble_pid", (uint16_t)pid);
    if (has_mdns) platform_cfg_set_str("mdns_host", mdns_host);
    if (has_fs)   platform_cfg_set_u8("cam_framesize", (uint8_t)fs);
    if (has_q)    platform_cfg_set_u8("cam_quality", (uint8_t)q);
    if (has_mu)   platform_cfg_set_u8("http_max_uri", (uint16_t)max_uri);
    if (has_rc)   platform_cfg_set_u16("http_resp_cap", (uint16_t)resp_cap);
    if (has_ssid) platform_cfg_set_wifi(wifi_ssid, has_pass ? wifi_pass : "");

    int n = snprintf(resp, cap, "{\"ok\":true,\"rebooting\":true}");
    if (n < 0 || (size_t)n >= cap) return 500;
    platform_request_reboot();   /* esp_restart()：写完后冷生效，函数不返回 */
    return 200;
}

/* ---- 控制 API 鉴权钩子（P0 地基；默认关，LAN 内使用）---- */
static bool g_control_auth = false;     /* true 时控制端点要求 Bearer */
static const char *g_admin_token = "";  /* 非空时作为有效令牌（P5 经 settings/启动参数设置） */

/* 控制端点调用：g_control_auth 关 → 直接放行；开 → 校验 "Bearer <token>"。
 * 返回 0 放行；非 0 = 应返回的 HTTP 状态码（403）。 */
static int require_auth(const char *auth)
{
    if (!g_control_auth) return 0;
    if (auth && g_admin_token && *g_admin_token) {
        size_t L = strlen(g_admin_token);
        if (strncmp(auth, "Bearer ", 7) == 0 &&
            strncmp(auth + 7, g_admin_token, L) == 0 &&
            (auth[7 + L] == '\0' || auth[7 + L] == ' '))
            return 0;
    }
    return 403;
}

/* ---- 自描述 schema（Agent 发现用）----
 *
 * 🔴 长度纪律（本轮 P5 修的真 bug）：本函数的响应必须**装得进** platform_net.c 的
 * RESP_CAP。snprintf 装不下时返回的是「本应写入的长度」，而下面收尾判的是
 * `n >= cap → return 500` —— 所以超长不是「被截断的坏 JSON」，而是**恒 500**，
 * 症状是 Agent 侧「schema 拉取失败、所有工具都没注册」，看不出是设备端长度问题。
 * 增删端点后必须跑 tests/test_control_contract.py::test_schema_response_fits_resp_cap。
 *
 * 与 shared/api_schema.yaml 是两份需人工同步的清单（同一守卫强制端点集合相等）。 */
static int handle_schema(char *resp, size_t cap)
{
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"base\":\"/api/v1\",\"auth\":{\"required\":%s,\"scheme\":\"Bearer\"},"
        "\"endpoints\":["
        "{\"path\":\"/health\",\"methods\":[\"GET\"],\"desc\":\"遥测\",\"params\":[]},"
        "{\"path\":\"/sysinfo\",\"methods\":[\"GET\"],\"desc\":\"存储/内存/栈\",\"params\":[]},"
        "{\"path\":\"/schema\",\"methods\":[\"GET\"],\"desc\":\"本文件\",\"params\":[]},"
        "{\"path\":\"/settings\",\"methods\":[\"GET\",\"POST\"],\"desc\":\"设备配置(冷生效)\",\"params\":[\"ble_name\",\"ble_vid\",\"ble_pid\",\"mdns_host\",\"cam_framesize\",\"cam_quality\",\"wifi_ssid\",\"wifi_pass\",\"http_resp_cap\",\"http_max_uri\"]},"
        "{\"path\":\"/led\",\"methods\":[\"GET\"],\"desc\":\"IO2灯调试\",\"params\":[\"io2\",\"pol\",\"mode\"]},"
        "{\"path\":\"/tap\",\"methods\":[\"POST\"],\"desc\":\"点按\",\"params\":[\"x\",\"y\",\"duration_ms\"]},"
        "{\"path\":\"/swipe\",\"methods\":[\"POST\"],\"desc\":\"拖拽\",\"params\":[\"hid_report_start\",\"hid_report_end\"]},"
        "{\"path\":\"/gesture\",\"methods\":[\"POST\"],\"desc\":\"手势\",\"params\":[\"type\",\"steps\",\"step_ms\",\"dx\",\"dy\",\"press\"]},"
        "{\"path\":\"/key\",\"methods\":[\"POST\"],\"desc\":\"按键\",\"params\":[\"key\"]},"
        "{\"path\":\"/calibrate/point\",\"methods\":[\"POST\"],\"desc\":\"标定点\",\"params\":[\"index\",\"cap_x\",\"cap_y\"]},"
        "{\"path\":\"/calibrate/commit\",\"methods\":[\"POST\"],\"desc\":\"标定提交\",\"params\":[\"device_id\"]},"
        "{\"path\":\"/camera\",\"methods\":[\"POST\"],\"desc\":\"相机参数\",\"params\":[\"exposure_auto\",\"exposure_value\",\"auto_wb\",\"auto_gain\",\"brightness\",\"contrast\",\"saturation\",\"frame_size\"]},"
        "{\"path\":\"/wifi/status\",\"methods\":[\"GET\"],\"desc\":\"WiFi状态\",\"params\":[]},"
        "{\"path\":\"/wifi/scan\",\"methods\":[\"GET\"],\"desc\":\"扫描AP(阻塞1-2s)\",\"params\":[]},"
        "{\"path\":\"/wifi/connect\",\"methods\":[\"POST\"],\"desc\":\"热连接(不落NVS)\",\"params\":[\"ssid\",\"pass\"]},"
        "{\"path\":\"/wifi/disconnect\",\"methods\":[\"POST\"],\"desc\":\"断开WiFi\",\"params\":[]},"
        "{\"path\":\"/ble/status\",\"methods\":[\"GET\"],\"desc\":\"BLE状态\",\"params\":[]},"
        "{\"path\":\"/ble/advertise\",\"methods\":[\"POST\"],\"desc\":\"广播开关\",\"params\":[\"on\"]},"
        "{\"path\":\"/ble/disconnect\",\"methods\":[\"POST\"],\"desc\":\"断开BLE\",\"params\":[]},"
        "{\"path\":\"/ble/bonds\",\"methods\":[\"GET\",\"POST\"],\"desc\":\"配对列表/遗忘\",\"params\":[\"id\"]},"
        "{\"path\":\"/hid\",\"methods\":[\"GET\",\"POST\"],\"desc\":\"HID 路由状态(GET)/策略(POST mode=auto|ble|usb)\",\"params\":[\"mode\"]},"
        "{\"path\":\"/hid/mouse\",\"methods\":[\"POST\"],\"desc\":\"鼠标report\",\"params\":[\"buttons\",\"dx\",\"dy\"]},"
        "{\"path\":\"/hid/keyboard\",\"methods\":[\"POST\"],\"desc\":\"键盘report\",\"params\":[\"modifier\",\"keys\"]},"
        "{\"path\":\"/hid/consumer\",\"methods\":[\"POST\"],\"desc\":\"Consumer report\",\"params\":[\"usage_lo\",\"usage_hi\"]},"
        "{\"path\":\"/gpio\",\"methods\":[\"GET\"],\"desc\":\"GPIO列表\",\"params\":[]},"
        "{\"path\":\"/gpio/{pin}\",\"methods\":[\"GET\",\"POST\"],\"desc\":\"单引脚\",\"params\":[\"mode\",\"value\"]},"
        "{\"path\":\"/events\",\"methods\":[\"GET\"],\"desc\":\"事件通道(WebSocket,双向;本GET仅返回订阅者数)\",\"params\":[]}"
        "]}",
        g_control_auth ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- WiFi 控制端点（P0 调用桩，真实逻辑 P2）---- */
static int handle_wifi_status(char *resp, size_t cap)
{
    char ip[16] = "0.0.0.0";
    platform_wifi_ip(ip, sizeof(ip));
    int n = snprintf(resp, cap,
        "{\"connected\":%d,\"ip\":\"%s\",\"rssi\":%d}",
        platform_wifi_is_connected() ? 1 : 0, ip, platform_wifi_rssi());
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_wifi_scan(char *resp, size_t cap)
{
    platform_wifi_scan();
    wifi_ap_t aps[16];
    int n = platform_wifi_scan_results(aps, 16);
    if (n < 0) n = 0;
    size_t used = 0;
    int r = snprintf(resp, cap, "{\"scanning\":true,\"count\":%d,\"ap\":[", n);
    if (r < 0 || (size_t)r >= cap) return 500;
    used = (size_t)r;
    for (int i = 0; i < n; i++) {
        int k = snprintf(resp + used, cap - used,
            "%s{\"ssid\":\"%s\",\"rssi\":%d,\"bssid\":\"%s\",\"auth\":%d}",
            i ? "," : "", aps[i].ssid, aps[i].rssi, aps[i].bssid, aps[i].auth);
        if (k < 0 || (size_t)k >= cap - used) return 500;
        used += (size_t)k;
    }
    r = snprintf(resp + used, cap - used, "]}");
    if (r < 0 || (size_t)r >= cap - used) return 500;
    return 200;
}
static int handle_wifi_connect(const char *body, char *resp, size_t cap)
{
    char ssid[64] = {0}, pass[64] = {0};
    int has = json_find_str(body, "ssid", ssid, sizeof(ssid));
    json_find_str(body, "pass", pass, sizeof(pass));
    if (!has || strlen(ssid) == 0)
        return settings_err(resp, cap, "ssid required");
    int rc = platform_wifi_connect(ssid, pass);
    int n = snprintf(resp, cap, "{\"ok\":%s,\"state\":\"connecting\"}", rc == 0 ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_wifi_disconnect(char *resp, size_t cap)
{
    platform_wifi_disconnect();
    int n = snprintf(resp, cap, "{\"ok\":true}");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- BLE 外设态控制端点（P0 调用桩，真实逻辑 P3）---- */
static int handle_ble_status(char *resp, size_t cap)
{
    char bonds[256] = "[]";
    platform_ble_list_bonds(bonds, sizeof(bonds));
    int n = snprintf(resp, cap,
        "{\"advertising\":%d,\"connected\":%d,\"bonds\":%s}",
        platform_ble_is_advertising() ? 1 : 0,
        platform_ble_is_connected() ? 1 : 0, bonds);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_ble_advertise(const char *body, char *resp, size_t cap)
{
    int on = 0;
    json_find_bool(body, "on", &on);
    platform_ble_advertise(on != 0);
    int n = snprintf(resp, cap, "{\"ok\":true,\"advertising\":%d}", on ? 1 : 0);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_ble_disconnect(char *resp, size_t cap)
{
    platform_ble_disconnect();
    int n = snprintf(resp, cap, "{\"ok\":true}");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_ble_bonds(const char *body, char *resp, size_t cap)
{
    long id = -1;
    if (json_find_int(body, "id", &id) && id >= 0) {
        int rc = platform_ble_forget_bond((int)id);
        int n = snprintf(resp, cap, "{\"ok\":%s}", rc == 0 ? "true" : "false");
        return (n < 0 || (size_t)n >= cap) ? 500 : 200;
    }
    char bonds[512] = "[]";
    platform_ble_list_bonds(bonds, sizeof(bonds));
    int n = snprintf(resp, cap, "{\"ok\":true,\"bonds\":%s}", bonds);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- HID 直发端点（包装通用 emit_report）---- */
static int handle_hid_mouse(const char *body, char *resp, size_t cap)
{
    long b = 0, dx = 0, dy = 0;
    json_find_int(body, "buttons", &b);
    json_find_int(body, "dx", &dx);
    json_find_int(body, "dy", &dy);
    uint8_t r[3] = { (uint8_t)(b & 0xFF), (uint8_t)((int8_t)dx), (uint8_t)((int8_t)dy) };
    int rc = hid_router_emit(HID_REPORT_ID_MOUSE, r, 3);
    int n = snprintf(resp, cap, "{\"ok\":%s}", rc == 0 ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_hid_keyboard(const char *body, char *resp, size_t cap)
{
    long mod = 0;
    int keys[6] = {0, 0, 0, 0, 0, 0};
    json_find_int(body, "modifier", &mod);
    json_find_array5(body, "keys", keys);
    uint8_t r[8] = { (uint8_t)(mod & 0xFF), 0,
                     (uint8_t)keys[0], (uint8_t)keys[1], (uint8_t)keys[2],
                     (uint8_t)keys[3], (uint8_t)keys[4], (uint8_t)keys[5] };
    int rc = hid_router_emit(HID_REPORT_ID_KEYBOARD, r, 8);
    int n = snprintf(resp, cap, "{\"ok\":%s}", rc == 0 ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}
static int handle_hid_consumer(const char *body, char *resp, size_t cap)
{
    long lo = 0, hi = 0;
    json_find_int(body, "usage_lo", &lo);
    json_find_int(body, "usage_hi", &hi);
    uint8_t r[2] = { (uint8_t)(lo & 0xFF), (uint8_t)(hi & 0xFF) };
    int rc = hid_router_emit(HID_REPORT_ID_CONSUMER, r, 2);
    int n = snprintf(resp, cap, "{\"ok\":%s}", rc == 0 ? "true" : "false");
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ── HID 路由状态 / 策略（现场诊断「按键没反应」的第一站）────────────
 * GET /api/v1/hid  → 当前策略、下一帧会走哪、两个后端的就绪态、统计。
 *
 * 为什么值得单独开端点：报文实际走哪条链路在上层是**看不见**的 ——
 * HTTP 调的是同一个 hid_router_emit，USB 挂了就走 USB、没挂就回落 BLE。
 *
 * ⚠️ active 与 last 的区别（曾把排查带偏过）：
 *   active = **下一帧会走哪个后端**（hid_router_peek_backend，纯查询不发帧）
 *   last   = **上一帧实际走了哪个**（hid_router_active_backend）
 * 一条都没发过时 last 恒为 "ble"，哪怕 USB 早就挂载了 —— 拿它当「当前
 * 生效后端」会看到「USB mounted=1 却显示 active=ble」，像路由选错了，
 * 其实只是没发过帧。所以 JSON 里 active 用 peek，last 单独放。
 *
 * 现场出现「按了没反应」时的判断顺序：
 *   1. dropped 是否在涨     —— 涨 = 两个后端都不可用或 send 失败（看 fallback）
 *   2. active / mounted     —— USB 未枚举时 AUTO 必然回落 BLE，
 *                              若连 BLE 也没连上，报文就是发出去也没人收
 *   3. fallback / would_fallback —— 涨 = USB 挂着但单个 send 失败在回落
 */
static int handle_hid_status(char *resp, size_t cap)
{
    hid_router_stats_t st;
    hid_router_stats(&st);
    int would_fallback = 0;
    hid_backend_t next = hid_router_peek_backend(&would_fallback);
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"policy\":\"%s\",\"active\":\"%s\","
        "\"last\":\"%s\",\"would_fallback\":%d,"
        "\"ble\":{\"registered\":%d,\"connected\":%d},"
        "\"usb\":{\"registered\":%d,\"mounted\":%d},"
        "\"stats\":{\"sent_ble\":%u,\"sent_usb\":%u,\"fallback\":%u,\"dropped\":%u}}",
        hid_router_policy_name(hid_router_get_policy()),
        hid_router_backend_name(next),
        hid_router_backend_name(hid_router_active_backend()),
        would_fallback,
        hid_router_is_registered(HID_BACKEND_BLE) ? 1 : 0,
        platform_ble_is_connected() ? 1 : 0,
        hid_router_is_registered(HID_BACKEND_USB) ? 1 : 0,
        platform_usb_ready(),
        (unsigned)st.sent[HID_BACKEND_BLE],
        (unsigned)st.sent[HID_BACKEND_USB],
        (unsigned)st.fallback,
        (unsigned)st.dropped);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* POST /api/v1/hid {"mode":"auto"|"ble"|"usb"} —— 强制某条链路（诊断用）。
 * ⚠️ USB_ONLY 下 USB 未挂载时**不回落**（hid_router 铁律 2）：
 *    宁可丢帧也不悄悄换链路，否则现场现象与配置不符更难排查。
 *    诊断完请记得调回 auto。 */
static int handle_hid_mode(const char *body, char *resp, size_t cap)
{
    char mode[16] = {0};
    hid_route_policy_t p = hid_router_get_policy();
    if (json_find_str(body, "mode", mode, sizeof(mode))) {
        if      (strcmp(mode, "auto") == 0) p = HID_ROUTE_AUTO;
        else if (strcmp(mode, "ble")  == 0) p = HID_ROUTE_BLE_ONLY;
        else if (strcmp(mode, "usb")  == 0) p = HID_ROUTE_USB_ONLY;
        else return 400;
        /* ⚠️ 全程不回落到内存；策略是全局状态，只在 HTTP 线程改（见 router 头注释）。 */
        hid_router_set_policy(p);
    }
    (void)mode;
    int n = snprintf(resp, cap, "{\"ok\":true,\"policy\":\"%s\"}",
                     hid_router_policy_name(hid_router_get_policy()));
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- GPIO 端点（P0 调用桩，真实驱动 P1）---- */
static int handle_gpio(char *resp, size_t cap)
{
    gpio_pin_info_t pins[32];
    int n = platform_gpio_list(pins, 32);
    if (n < 0) n = 0;
    size_t used = 0;
    int r = snprintf(resp, cap, "{\"ok\":true,\"pins\":[");
    if (r < 0 || (size_t)r >= cap) return 500;
    used = (size_t)r;
    for (int i = 0; i < n; i++) {
        const char *m = pins[i].mode == GPIO_MODE_IN ? "in"
                       : pins[i].mode == GPIO_MODE_PWM ? "pwm" : "out";
        int k = snprintf(resp + used, cap - used,
            "%s{\"pin\":%d,\"mode\":\"%s\",\"value\":%d}", i ? "," : "",
            pins[i].pin, m, pins[i].value);
        if (k < 0 || (size_t)k >= cap - used) return 500;
        used += (size_t)k;
    }
    r = snprintf(resp + used, cap - used, "]}");
    if (r < 0 || (size_t)r >= cap - used) return 500;
    return 200;
}
/* 单引脚路由：对应 platform_net.c ROUTES[] 里的通配条目 /api/v1/gpio/ 加 '*' 前缀
 * （ESP-IDF httpd 用 '*' 作通配，匹配 /api/v1/gpio/{pin}）。
 * 13 = strlen("/api/v1/gpio/")，即去掉通配符后的前缀长度。 */
static const char *GPIO_PIN_ROUTE = "/api/v1/gpio/*";

static int handle_gpio_pin(const char *path, const char *body, char *resp, size_t cap)
{
    const char *p = strrchr(path, '/');
    int pin = p ? atoi(p + 1) : -1;
    if (pin < 0) return settings_err(resp, cap, "invalid pin");
    char mode[8] = {0};
    long val = 0;
    json_find_str(body, "mode", mode, sizeof(mode));
    int has_val = json_find_int(body, "value", &val);
    if (mode[0]) {
        gpio_mode_e m = GPIO_MODE_IN;
        if (strcmp(mode, "out") == 0)      m = GPIO_MODE_OUT;
        else if (strcmp(mode, "pwm") == 0) m = GPIO_MODE_PWM;
        else if (strcmp(mode, "in") != 0)  return settings_err(resp, cap, "mode must be in/out/pwm");
        if (platform_gpio_pin_mode(pin, m) != 0)
            return settings_err(resp, cap, "pin not allowed / mode set failed");
    }
    if (has_val) {
        int is_pwm = (mode[0] && strcmp(mode, "pwm") == 0);
        int rc = is_pwm ? platform_gpio_pwm(pin, (int)val)
                        : platform_gpio_write(pin, (int)val);
        if (rc != 0) return settings_err(resp, cap, "write failed");
    }
    gpio_pin_info_t info;
    if (platform_gpio_get(pin, &info) != 0)
        return settings_err(resp, cap, "pin not manageable");
    const char *m = info.mode == GPIO_MODE_IN ? "in"
                   : info.mode == GPIO_MODE_PWM ? "pwm" : "out";
    int n = snprintf(resp, cap, "{\"ok\":true,\"pin\":%d,\"mode\":\"%s\",\"value\":%d}",
                     pin, m, info.value);
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

/* ---- 事件流（P4 用 WebSocket 实现，P0 占位）---- */
/* GET /api/v1/events —— 事件通道状态。
 * 注意：真正的订阅走 WebSocket 升级（GET 带 Upgrade: websocket，
 * esp32 侧由 platform_net.c 的 events_ws_handler 单独注册，不走本函数）。
 * 这里仅返回当前订阅者数，供 Agent 侧做可达性自检。 */
static int handle_events(char *resp, size_t cap)
{
    int n = snprintf(resp, cap,
        "{\"ok\":true,\"transport\":\"websocket\",\"subscribers\":%d,"
        "\"hint\":\"connect with Upgrade: websocket to subscribe\"}",
        platform_events_subscriber_count());
    return (n < 0 || (size_t)n >= cap) ? 500 : 200;
}

int routes_handle(http_method_t method, const char *path, const char *body,
                  const char *auth, char *resp, size_t cap)
{
    if (!resp || cap == 0) return 500;
    resp[0] = '\0';

    /* 公开端点（只读/发现）不需要鉴权 */
    if (method == M_GET && strcmp(path, "/api/v1/health") == 0)
        return handle_health(resp, cap);
    if (method == M_GET && strcmp(path, "/api/v1/sysinfo") == 0)
        return handle_sysinfo(resp, cap);
    if (method == M_GET && strcmp(path, "/api/v1/settings") == 0)
        return handle_settings(body, resp, cap);
    if (method == M_GET && strcmp(path, "/api/v1/schema") == 0)
        return handle_schema(resp, cap);
    /* LED 调试端点带可选 query（?io2=...），故用前缀匹配而非全等比较 */
    if (method == M_GET && strncmp(path, "/api/v1/led", 11) == 0) {
        const char *tail = path + 11;
        if (*tail == '\0' || *tail == '?')
            return handle_led(path, resp, cap);
    }

    /* ---- 以下为控制端点：经鉴权钩子 ---- */
    int auth_rc = require_auth(auth);
    if (auth_rc != 0) {
        int n = snprintf(resp, cap, "{\"ok\":false,\"error\":\"unauthorized\"}");
        if (n < 0 || (size_t)n >= cap) return 500;
        return auth_rc;
    }

    if (method == M_GET) {
        if (strcmp(path, "/api/v1/wifi/status") == 0)  return handle_wifi_status(resp, cap);
        if (strcmp(path, "/api/v1/wifi/scan") == 0)     return handle_wifi_scan(resp, cap);
        if (strcmp(path, "/api/v1/ble/status") == 0)    return handle_ble_status(resp, cap);
        if (strcmp(path, "/api/v1/ble/bonds") == 0)     return handle_ble_bonds(body, resp, cap);
        if (strcmp(path, "/api/v1/gpio") == 0)          return handle_gpio(resp, cap);
        if (strncmp(path, GPIO_PIN_ROUTE, 13) == 0)
            return handle_gpio_pin(path, body, resp, cap);
        if (strcmp(path, "/api/v1/events") == 0)        return handle_events(resp, cap);
        if (strcmp(path, "/api/v1/hid") == 0)             return handle_hid_status(resp, cap);
    }
    if (method == M_POST) {
        if (strcmp(path, "/api/v1/tap") == 0)              return handle_tap(body, resp, cap);
        if (strcmp(path, "/api/v1/gesture") == 0)          return handle_gesture(body, resp, cap);
        if (strcmp(path, "/api/v1/swipe") == 0)            return handle_swipe(body, resp, cap);
        if (strcmp(path, "/api/v1/key") == 0)              return handle_key(body, resp, cap);
        if (strcmp(path, "/api/v1/calibrate/point") == 0)  return handle_calib_point(body, resp, cap);
        if (strcmp(path, "/api/v1/calibrate/commit") == 0) return handle_calib_commit(body, resp, cap);
        if (strcmp(path, "/api/v1/camera") == 0)           return handle_camera(body, resp, cap);
        if (strcmp(path, "/api/v1/settings") == 0)         return handle_settings(body, resp, cap);
        if (strcmp(path, "/api/v1/wifi/connect") == 0)     return handle_wifi_connect(body, resp, cap);
        if (strcmp(path, "/api/v1/wifi/disconnect") == 0)  return handle_wifi_disconnect(resp, cap);
        if (strcmp(path, "/api/v1/ble/advertise") == 0)    return handle_ble_advertise(body, resp, cap);
        if (strcmp(path, "/api/v1/ble/disconnect") == 0)   return handle_ble_disconnect(resp, cap);
        if (strcmp(path, "/api/v1/ble/bonds") == 0)        return handle_ble_bonds(body, resp, cap);
        if (strcmp(path, "/api/v1/hid/mouse") == 0)        return handle_hid_mouse(body, resp, cap);
        if (strcmp(path, "/api/v1/hid/keyboard") == 0)     return handle_hid_keyboard(body, resp, cap);
        if (strcmp(path, "/api/v1/hid/consumer") == 0)     return handle_hid_consumer(body, resp, cap);
        if (strcmp(path, "/api/v1/hid") == 0)             return handle_hid_mode(body, resp, cap);
        /* 单引脚 GPIO 写：POST /api/v1/gpio/{pin} 设 mode/value（handler 同时兼容 GET 读）*/
        if (strncmp(path, GPIO_PIN_ROUTE, 13) == 0)
            return handle_gpio_pin(path, body, resp, cap);
    }
    return 404;
}
