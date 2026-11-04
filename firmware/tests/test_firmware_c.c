/*
 * tests/test_firmware_c.c — 固件纯逻辑 host 单元测试（需 gcc，见 Makefile）
 *
 * 覆盖：hid_report 相对鼠标打包/夹取、ble_hid 透传与 swipe（相对增量）、
 *       gesture 几何展开（闭合相对环）、mjpeg 边界帧、http_api 合约解析。
 * 全部不触达硬件（走 platform/host 桩）。
 */
#include "tests/test_runner.h"

#include "ble_hid/hid_report.h"
#include "ble_hid/ble_hid.h"
#include "camera/mjpeg.h"
#include "main/http_api.h"
#include "main/routes.h"      /* QSHOU_STREAM_BOUNDARY：边界串单点定义 */
#include "hid_router/hid_router.h"
#include "platform/platform.h"
#include "platform/host/host_test_stubs.h"

#include <string.h>

/* 可移植的子串查找（替代 memmem，后者非标准） */
static const uint8_t *memfind(const uint8_t *hay, size_t hlen,
                              const uint8_t *needle, size_t nlen)
{
    if (nlen == 0 || nlen > hlen) return NULL;
    for (size_t i = 0; i + nlen <= hlen; i++) {
        if (memcmp(hay + i, needle, nlen) == 0)
            return hay + i;
    }
    return NULL;
}

/* ---- hid_report：3 字节相对坐标 ---- */
static void test_hid_pack(void)
{
    uint8_t rep[3];
    int n = hid_pack_report(rep, HID_BTN_LEFT, 5, -20);
    TR_CHECK(n == HID_REPORT_LEN);
    TR_CHECK(rep[0] == HID_BTN_LEFT);
    TR_CHECK(rep[1] == 5);
    TR_CHECK(rep[2] == (uint8_t)(int8_t)-20);   /* 二进制补码 0xEC */

    /* 增量夹取 */
    TR_CHECK(hid_clamp_delta(200)  == HID_DELTA_MAX);
    TR_CHECK(hid_clamp_delta(-200) == HID_DELTA_MIN);

    /* 长度校验：4 字节视为非法 */
    uint8_t bad[4] = {1, 2, 3, 4};
    TR_CHECK(hid_report_valid(bad, 4) == 0);
    TR_CHECK(hid_report_valid(rep, 3) == 1);
}

/* ---- mjpeg ---- */
static void test_mjpeg_part(void)
{
    static const uint8_t jpeg[] = {0xFF,0xD8,0xAB,0xCD,0xFF,0xD9};
    uint8_t buf[128];
    size_t n = mjpeg_part(buf, sizeof(buf), QSHOU_STREAM_BOUNDARY, jpeg, sizeof(jpeg));
    TR_CHECK(n > 0);
    /* 边界串取 routes.h 的单点定义，别在测试里硬编码品牌名——
     * 2026-10-25 更名 Y71t→Qshou 时这里还留着 "--Y7" 前缀，
     * 导致改名后 C 测试一直红（代码其实是对的）。 */
    {
        const char *bd = "--" QSHOU_STREAM_BOUNDARY;
        TR_CHECK(memcmp(buf, bd, strlen(bd)) == 0);
    }
    TR_CHECK(memfind(buf, n, (const uint8_t*)"Content-Type: image/jpeg", 22) != NULL);
    TR_CHECK(buf[n-2]=='\r' && buf[n-1]=='\n');
    TR_CHECK(memfind(buf, n, jpeg, sizeof(jpeg)) != NULL);

    uint8_t small[4];
    TR_CHECK(mjpeg_part(small, sizeof(small), QSHOU_STREAM_BOUNDARY, jpeg, sizeof(jpeg)) == 0);
}

/* ---- http_api：合约级解析 ---- */
static void test_http_api_tap(void)
{
    const char *body = "{\"x\":540,\"y\":1200,\"hid_report\":[3,19,136,46,224],"
                       "\"hid_x\":5000,\"hid_y\":2400}";
    tap_req_t req;
    TR_CHECK(http_parse_tap(body, &req) == 0);
    TR_CHECK(req.has_report == 1);
    TR_CHECK(req.hid_report[0]==3 && req.hid_report[1]==19 && req.hid_report[2]==136);
    TR_CHECK(req.hid_report[3]==46 && req.hid_report[4]==224);
    TR_CHECK(req.scr_x==540 && req.scr_y==1200);

    TR_CHECK(http_parse_tap("{\"x\":1,\"y\":2}", &req) == -1);
}

static void test_http_api_swipe(void)
{
    const char *body = "{\"x0\":0,\"y0\":0,\"x1\":1080,\"y1\":2400,"
        "\"hid_report_start\":[3,0,0,0,0],\"hid_report_end\":[3,39,16,93,208]}";
    swipe_req_t req;
    TR_CHECK(http_parse_swipe(body, &req) == 0);
    TR_CHECK(req.has_start && req.has_end);
    TR_CHECK(req.hid_report_start[0]==3 && req.hid_report_end[1]==39);
}

static void test_http_api_calib_key(void)
{
    calib_point_req_t cp;
    TR_CHECK(http_parse_calib_point("{\"index\":2,\"cap_x\":100,\"cap_y\":200}", &cp) == 0);
    TR_CHECK(cp.index==2 && cp.cap_x==100.0f && cp.cap_y==200.0f);

    calib_commit_req_t cc;
    http_parse_calib_commit("{\"device_id\":\"qshou-01\"}", &cc);
    TR_CHECK(cc.has_device && strcmp(cc.device_id, "qshou-01")==0);

    key_req_t k;
    TR_CHECK(http_parse_key("{\"key\":\"HOME\"}", &k) == 0 && strcmp(k.key,"HOME")==0);
}

/* ---- ble_hid 透传 + gesture 几何（相对增量，依赖 host 桩） ---- */
/* ⚠️ USB 改造后 ble_hid_emit_report 经 hid_router 选后端（USB 优先、BLE 回落），
 *   不再直接落 host 历史缓冲。因此必须先注册 BLE 后端 —— 与 main.c 的接线
 *   完全一致：hid_router_register(BLE, ready, platform_ble_emit_report)。
 *   早期版本的测试按「改造前不经路由」的拓扑写，直接断历史缓冲，会假红。 */
static int host_ready_always(void) { return 1; }

static void test_ble_hid_emit_and_gesture(void)
{
    hid_router_register(HID_BACKEND_BLE, host_ready_always, platform_ble_emit_report);
    hid_router_set_policy(HID_ROUTE_AUTO);
    hid_router_reset_stats();

    host_ble_reset();
    uint8_t rep[3];
    hid_pack_report(rep, HID_BTN_LEFT, 10, 10);
    TR_CHECK(ble_hid_emit_report(rep, 3) == 0);
    uint8_t got[3]; int len;
    int total = host_ble_get_nth(0, got, &len);
    TR_CHECK(total == 1 && len == 3 && memcmp(got, rep, 3) == 0);

    /* 长度错误应被拒绝（帧校验先于路由，不进统计） */
    uint8_t bad[4] = {1,2,3,4};
    TR_CHECK(ble_hid_emit_report(bad, 4) == -1);

    /* gesture box：相对增量闭合环，首尾按钮态正确 */
    host_ble_reset();
    gesture_spec_t spec = { GESTURE_BOX, 12, 90, 0, 0 };
    gesture_pt_t pts[256];
    int n = ble_hid_build_gesture_points(&spec, pts, 256);
    TR_CHECK(n > 0);
    TR_CHECK(pts[0].buttons == HID_BTN_LEFT);
    TR_CHECK(pts[n-1].buttons == 0);
    int sum_x = 0, sum_y = 0;
    for (int i = 0; i < n; i++) { sum_x += pts[i].dx; sum_y += pts[i].dy; }
    TR_CHECK(sum_x == 0 && sum_y == 0);   /* 闭合：回到起点 */

    /* host 执行器 play_gesture：同步逐帧播放，帧数与几何点数一一对应，
     * 每帧都经路由落在 host 历史缓冲（证明没有绕过路由器）。
     * 注意 host 历史只留最近 16 次发射（box 约 50 帧，索引会绕回），
     * 读「最新一帧」要用 clamp 后的下标，不能直接写 n-1。 */
    host_ble_reset();
    TR_CHECK(ble_hid_play_gesture(&spec) == 0);
    TR_CHECK(host_ble_get_nth(0, NULL, NULL) == n);
    {
        int avail = n < 16 ? n : 16;          /* host_ble_get_nth 可见的窗口 */
        TR_CHECK(host_ble_get_nth(avail - 1, got, &len) == n && len == 3 && got[0] == 0);
    }

    /* 参数错误：NULL / 未知键名 → <0（路由层据此映射 400） */
    TR_CHECK(ble_hid_play_gesture(NULL) == -1);
    TR_CHECK(ble_hid_send_key("NOPE_NOT_A_KEY") == -1);
    TR_CHECK(ble_hid_send_key(NULL) == -1);

    /* host 执行器 send_key：按下+抬起两帧，RID 走键盘/Consumer（不是鼠标） */
    host_ble_reset();
    TR_CHECK(ble_hid_send_key("HOME") == 0);           /* Consumer, RID 3 */
    TR_CHECK(host_ble_get_nth(0, NULL, NULL) == 2);
    TR_CHECK(host_ble_get_nth_rid(0) == HID_REPORT_ID_CONSUMER);
    TR_CHECK(host_ble_get_nth_rid(1) == HID_REPORT_ID_CONSUMER);

    host_ble_reset();
    TR_CHECK(ble_hid_send_key("A") == 0);              /* 键盘, RID 2 */
    TR_CHECK(host_ble_get_nth(0, NULL, NULL) == 2);
    TR_CHECK(host_ble_get_nth_rid(0) == HID_REPORT_ID_KEYBOARD);
}

/* ---- gesture 几何：闭合相对环 ---- */
static void test_ble_hid_gesture_box(void)
{
    gesture_spec_t spec = { GESTURE_BOX, 12, 90 };
    gesture_pt_t pts[256];
    int n = ble_hid_build_gesture_points(&spec, pts, 256);
    TR_CHECK(n > 0);
    TR_CHECK(pts[0].buttons == HID_BTN_LEFT);
    TR_CHECK(pts[n-1].buttons == 0);
    int sum_x = 0, sum_y = 0;
    for (int i = 0; i < n; i++) {
        sum_x += pts[i].dx;
        sum_y += pts[i].dy;
    }
    TR_CHECK(sum_x == 0 && sum_y == 0);  /* 闭合：回到起点 */
}

/* ---- gesture tap：1px 净零抖动锚定点击 ---- */
static void test_ble_hid_gesture_tap(void)
{
    gesture_spec_t spec = { GESTURE_TAP, 0, 80 };
    gesture_pt_t pts[256];
    int n = ble_hid_build_gesture_points(&spec, pts, 256);
    TR_CHECK(n > 0);
    TR_CHECK(pts[0].buttons == HID_BTN_LEFT);   /* 第 0 帧按下 */
    TR_CHECK(pts[n-1].buttons == 0);            /* 末帧抬起（不卡住按键） */
    int sum_x = 0, sum_y = 0;
    for (int i = 0; i < n; i++) { sum_x += pts[i].dx; sum_y += pts[i].dy; }
    TR_CHECK(sum_x == 0 && sum_y == 0);         /* 净位移 0：光标不漂移 */
    /* 第 1 帧必须用非零位移锚定按下（否则安卓丢弃零位移 down/up） */
    TR_CHECK((pts[1].dx != 0 || pts[1].dy != 0) && pts[1].buttons == HID_BTN_LEFT);
}

/* 复合 HID：键盘(RID 2) / Consumer(RID 3) 打包 + 名称映射 */
static void test_hid_pack_key(void)
{
    /* 键盘 report：8 字节 [modifier][reserved][k0..k5] */
    uint8_t kb[HID_KEYBOARD_REPORT_LEN];
    uint8_t keys[6] = { 0x04, 0, 0, 0, 0, 0 };   /* 'a' */
    TR_CHECK(hid_pack_keyboard(kb, HID_MOD_LSHIFT, keys) == HID_KEYBOARD_REPORT_LEN);
    TR_CHECK(kb[0] == HID_MOD_LSHIFT);
    TR_CHECK(kb[1] == 0);
    TR_CHECK(kb[2] == 0x04 && kb[7] == 0);

    /* Consumer report：2 字节 16-bit usage 小端（HOME = 0x0223） */
    uint8_t cc[HID_CONSUMER_REPORT_LEN];
    TR_CHECK(hid_pack_consumer(cc, 0x0223) == HID_CONSUMER_REPORT_LEN);
    TR_CHECK(cc[0] == 0x23 && cc[1] == 0x02);
    TR_CHECK(hid_pack_consumer(cc, 0) == HID_CONSUMER_REPORT_LEN);
    TR_CHECK(cc[0] == 0 && cc[1] == 0);

    /* 名称 → (rid, 按下/抬起帧) 映射 */
    uint8_t rid = 0, len = 0, pr[HID_KEY_FRAME_MAX], rl[HID_KEY_FRAME_MAX];
    TR_CHECK(ble_hid_key_frames("HOME", &rid, &len, pr, rl) == 0);
    TR_CHECK(rid == HID_REPORT_ID_CONSUMER && len == HID_CONSUMER_REPORT_LEN);
    TR_CHECK(pr[0] == 0x23 && pr[1] == 0x02);
    TR_CHECK(rl[0] == 0 && rl[1] == 0);          /* 抬起帧全 0（键不卡住） */

    TR_CHECK(ble_hid_key_frames("a", &rid, &len, pr, rl) == 0);
    TR_CHECK(rid == HID_REPORT_ID_KEYBOARD && len == HID_KEYBOARD_REPORT_LEN);
    TR_CHECK(pr[0] == 0 && pr[2] == 0x04);       /* 小写无修饰键 */

    TR_CHECK(ble_hid_key_frames("A", &rid, &len, pr, rl) == 0);
    TR_CHECK(pr[0] == HID_MOD_LSHIFT && pr[2] == 0x04);  /* 大写加 Shift */

    TR_CHECK(ble_hid_key_frames("NOPE_NOT_A_KEY", &rid, &len, pr, rl) == -1);
}

/* ---- hid_router：peek（下一帧会走哪）必须与实走同一条选择路径 ---- */
/* ⚠️ 这组用例来自实车误判：USB 已挂载、一帧没发过时 /api/v1/hid 的 active
 *   显示 "ble"（因为 active_backend 返回的是 s_last 的初值），看着像路由
 *   选错。故 peek 与实走必须复用同一个 select_backend，且这里把
 *   「peek 不动统计、不发帧、last 只在真发过才更新」全部钉死。 */
static int s_fake_ready = 1;
static uint8_t s_fake_rx[16];
static size_t  s_fake_rx_len = 0;
static int     s_fake_send_calls = 0;

static int stub_ready(void) { return s_fake_ready; }
static int stub_send(uint8_t report_id, const uint8_t *data, size_t len)
{
    (void)report_id;
    s_fake_send_calls++;
    if (len == 0 || len > sizeof(s_fake_rx)) return -1;
    memcpy(s_fake_rx, data, len);
    s_fake_rx_len = len;
    return 0;
}
static int stub_send_fail(uint8_t report_id, const uint8_t *data, size_t len)
{
    (void)report_id; (void)data; (void)len;
    s_fake_send_calls++;
    return -1;      /* 故意失败：用来验证「失败也不换后端重试」 */
}

static void test_hid_router_peek(void)
{
    hid_router_stats_t st;
    uint8_t d[3] = {0, 1, 2};

    /* 先回到「零后端」干净态，避免被别的用例影响 */
    hid_router_register(HID_BACKEND_BLE,  NULL, NULL);
    hid_router_register(HID_BACKEND_USB,  NULL, NULL);
    hid_router_set_policy(HID_ROUTE_AUTO);
    hid_router_reset_stats();

    /* ① 一个后端都没注册：AUTO 只能给 BLE，且不算「回落」（那是唯一选择） */
    int fb = -1;
    TR_CHECK(hid_router_peek_backend(&fb) == HID_BACKEND_BLE);
    TR_CHECK(fb == 0);

    /* ② 两个后端都注册、USB 可用 → peek 立即给 USB。
     *    ⚠️ NULL 出参回归点：hid_router_peek_backend(NULL) 曾在这里崩
     *    （select_backend 无条件 `*is_fallback = 0` 空指针写）。
     *    头文件写明「可为 NULL」，实机 routes.c 一直传合法指针所以没暴露。 */
    hid_router_register(HID_BACKEND_BLE, stub_ready, stub_send);
    hid_router_register(HID_BACKEND_USB, stub_ready, stub_send);
    TR_CHECK(hid_router_peek_backend(NULL) == HID_BACKEND_USB);

    /* ★ 核心：一条都没发过时 peek 也必须是 USB（旧实现这里给 BLE） */
    TR_CHECK(hid_router_active_backend() == HID_BACKEND_BLE);   /* last 仍是初值 */

    /* ③ USB 不可用 → 回落 BLE，is_fallback = 1 */
    s_fake_ready = 0;
    fb = -1;
    TR_CHECK(hid_router_peek_backend(&fb) == HID_BACKEND_BLE);
    TR_CHECK(fb == 1);

    /* ④ 策略可覆盖优先关系 */
    hid_router_set_policy(HID_ROUTE_BLE_ONLY);
    TR_CHECK(hid_router_peek_backend(NULL) == HID_BACKEND_BLE);
    hid_router_set_policy(HID_ROUTE_USB_ONLY);
    TR_CHECK(hid_router_peek_backend(NULL) == HID_BACKEND_USB);
    hid_router_set_policy(HID_ROUTE_AUTO);

    /* ⑤ peek 是纯查询：不得发帧、不得动统计 */
    s_fake_send_calls = 0;
    (void)hid_router_peek_backend(NULL);
    TR_CHECK(s_fake_send_calls == 0);
    hid_router_stats(&st);
    TR_CHECK(st.sent[HID_BACKEND_BLE] == 0 && st.sent[HID_BACKEND_USB] == 0);
    TR_CHECK(st.fallback == 0 && st.dropped == 0);

    /* ⑥ peek 与实走必须一致：USB 可用时 peek 给 USB，emit 也真的发到 USB */
    s_fake_ready = 1;
    hid_backend_t next = hid_router_peek_backend(NULL);
    TR_CHECK(next == HID_BACKEND_USB);
    s_fake_send_calls = 0;
    TR_CHECK(hid_router_emit(1, d, 3) == 0);
    TR_CHECK(s_fake_send_calls == 1);                          /* 只发一次 */
    TR_CHECK(hid_router_active_backend() == next);             /* last 追上 peek */
    hid_router_stats(&st);
    TR_CHECK(st.sent[HID_BACKEND_USB] == 1 && st.sent[HID_BACKEND_BLE] == 0);
    TR_CHECK(st.fallback == 0);                                /* 没回落 */

    /* ⑦ 真正的回落：USB 不可用时 emit 走 BLE，fallback +1，last 变 BLE */
    s_fake_ready = 0;
    TR_CHECK(hid_router_emit(1, d, 3) == 0);
    hid_router_stats(&st);
    TR_CHECK(st.sent[HID_BACKEND_BLE] == 1);
    TR_CHECK(st.fallback == 1);                                /* 记上回落 */
    TR_CHECK(hid_router_active_backend() == HID_BACKEND_BLE);
    TR_CHECK(hid_router_peek_backend(NULL) == HID_BACKEND_BLE); /* peek 也同意 */

    /* ⑧ send 失败不得换后端重试（铁律 1：严禁双发）。
     *    这里让**选中**的 USB send 必失败，而 BLE 明明可用 ——
     *    若谁加了「失败换个后端再试」，s_fake_send_calls 会变 2，立刻红。 */
    hid_router_reset_stats();
    s_fake_send_calls = 0;
    s_fake_ready = 1;
    hid_router_register(HID_BACKEND_USB, stub_ready, stub_send_fail);
    hid_router_register(HID_BACKEND_BLE, stub_ready, stub_send);
    TR_CHECK(hid_router_emit(1, d, 3) == -1);          /* 整体失败 */
    TR_CHECK(s_fake_send_calls == 1);                  /* 只调一次 send */
    hid_router_stats(&st);
    TR_CHECK(st.dropped == 1);
    TR_CHECK(st.sent[HID_BACKEND_USB] == 0 && st.sent[HID_BACKEND_BLE] == 0);
    TR_CHECK(st.fallback == 0);                        /* send 失败不计回落 */
    TR_CHECK(hid_router_active_backend() == HID_BACKEND_BLE);  /* last 不追失败路径 */

    /* 清理：注销两个后端，避免污染后续用例 */
    hid_router_register(HID_BACKEND_BLE, NULL, NULL);
    hid_router_register(HID_BACKEND_USB, NULL, NULL);
}

int main(void)
{
    /* 测试输出不留缓冲：崩溃（SIGILL/SIGSEGV）时已跑过的用例名必须可见，
     * 否则定位只能靠猜。 */
    setvbuf(stdout, NULL, _IONBF, 0);
    TR_RUN(test_hid_pack);
    TR_RUN(test_hid_pack_key);
    TR_RUN(test_mjpeg_part);
    TR_RUN(test_http_api_tap);
    TR_RUN(test_http_api_swipe);
    TR_RUN(test_http_api_calib_key);
    TR_RUN(test_ble_hid_emit_and_gesture);
    TR_RUN(test_ble_hid_gesture_box);
    TR_RUN(test_ble_hid_gesture_tap);
    TR_RUN(test_hid_router_peek);
    TR_SUMMARY();
}
