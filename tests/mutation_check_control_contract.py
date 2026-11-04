"""反向验证：逐条破坏，确认 tests/test_control_contract.py 的守卫会红。

纪律来源：本项目吃过「assertIn(token) 守卫被注释/同名标识符蒙混过关」的亏，
也吃过「守卫恒绿等于没有守卫」的亏。故每条守卫都必须做**变异测试**：
故意制造它要防的那个错误，确认测试变红；确认完再还原。

用法：python tests/mutation_check_control_contract.py
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TEST = os.path.join(_ROOT, "tests", "test_control_contract.py")

ROUTES_C = os.path.join(_ROOT, "firmware", "main", "routes.c")
NET_C = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_net.c")
GPIO_ESP = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_gpio.c")
GPIO_HOST = os.path.join(_ROOT, "firmware", "platform", "host", "platform_gpio.c")
SCHEMA_Y = os.path.join(_ROOT, "shared", "api_schema.yaml")

# (描述, 文件, 原片段, 替换片段, 期望失败的测试名子串)
MUTATIONS = [
    ("schema 响应超长 → 恒 500（P5 真 bug）",
     ROUTES_C,
     '"{\\"path\\":\\"/events\\"',
     '"{\\"path\\":\\"/events_with_a_very_long_padding_string_to_exceed_resp_cap\\"',
     "test_schema_response_fits_resp_cap"),

    ("自描述 JSON 少一个逗号 → 非法 JSON",
     ROUTES_C,
     '"{\\"path\\":\\"/sysinfo\\",\\"methods\\":[\\"GET\\"]',
     '"{\\"path\\":\\"/sysinfo\\" \\"methods\\":[\\"GET\\"]',
     "test_schema_json_literal_is_wellformed"),

    ("YAML 少登记一个端点 → Agent 隐形",
     SCHEMA_Y,
     '  - path: "/hid/consumer"',
     '  - path: "/hid/consumer_renamed"',
     "test_schema_yaml_matches_device_self_description"),

    ("YAML 裸键 on 被 YAML 解析成布尔 True",
     SCHEMA_Y,
     '      "on": "bool',
     '      on: "bool',
     "test_no_yaml_boolean_key_traps"),

    ("控制器坐标语义塞回 /swipe 的 request（固件静默忽略）",
     SCHEMA_Y,
     '      hid_report_start: "int[5]  # 起点 5 字节 waypoint',
     '      x0: "int  # 控制器侧光标当前 X"\n'
     '      hid_report_start: "int[5]  # 起点 5 字节 waypoint',
     "test_no_controller_only_params_left_in_firmware_request"),

    ("多余字段登记为 ignored_params 却不在 note 解释",
     SCHEMA_Y,
     '      hid_report_start: "int[5]  # 起点 5 字节 waypoint',
     '      x0: "int  # 控制器侧光标当前 X"\n'
     '      hid_report_start: "int[5]  # 起点 5 字节 waypoint'
     '"\n    ignored_params: ["x0"]',
     "test_no_controller_only_params_left_in_firmware_request"),

    ("auth 头写回从未实现的 X-Auth-Token",
     SCHEMA_Y,
     '  header: "Authorization"',
     '  header: "X-Auth-Token"',
     "test_auth_scheme_matches_implementation"),

    ("公开端点被挪到鉴权门之后",
     ROUTES_C,
     '    if (method == M_GET && strcmp(path, "/api/v1/schema") == 0)\n'
     '        return handle_schema(resp, cap);\n',
     '',
     "test_public_endpoints_dispatched_before_auth_gate"),

    ("鉴权钩子默认打开（上市阻断级）",
     ROUTES_C,
     "static bool g_control_auth = false",
     "static bool g_control_auth = true",
     "test_auth_disabled_by_default"),

    ("Authorization 头不再透传",
     NET_C,
     'httpd_req_get_hdr_value_str(req, "Authorization", auth_buf, sizeof(auth_buf));',
     'httpd_req_get_hdr_value_str(req, "X-Nothing", auth_buf, sizeof(auth_buf));',
     "test_authorization_header_forwarded_by_bridge"),

    ("GPIO 白名单放进 MSPI 占用脚 26",
     GPIO_ESP,
     "static const int GPIO_SAFE[] = {19, 21, 22, 23, 25};",
     "static const int GPIO_SAFE[] = {19, 21, 22, 23, 25, 26};",
     "test_safe_set_excludes_mspi_and_camera_pins"),

    ("GPIO 白名单放进相机占用脚 4",
     GPIO_HOST,
     "static const int GPIO_SAFE[] = {19, 21, 22, 23, 25};",
     "static const int GPIO_SAFE[] = {19, 21, 22, 23, 25, 4};",
     "test_both_seams_share_the_same_safe_set"),

    ("MSPI 守卫范围收窄（漏 D4..DQS）",
     GPIO_ESP,
     "return (pin >= 26 && pin <= 37);",
     "return (pin >= 26 && pin <= 33);",
     "test_mspi_guard_covers_full_26_37_range"),

    ("pin_mode 入口去掉放行校验",
     GPIO_ESP,
     "if (!gpio_pin_allowed(pin)) return -1;",
     "",
     "test_mode_set_entry_rejects_disallowed_pins"),

    ("httpd 通配 URI 匹配被关掉",
     NET_C,
     "cfg.uri_match_fn = httpd_uri_match_wildcard;",
     "cfg.uri_match_fn = httpd_uri_match_simple;",
     "test_gpio_pin_registered_as_wildcard"),

    ("POST 分支漏掉 GPIO_PIN_ROUTE（P1 真实漏项）",
     ROUTES_C,
     "        if (strncmp(path, GPIO_PIN_ROUTE, 13) == 0)\n"
     "            return handle_gpio_pin(path, body, resp, cap);\n    }\n    return 404;",
     "    }\n    return 404;",
     "test_post_gpio_pin_dispatch_present"),

    ("/events 被塞进通用 ROUTES[]",
     NET_C,
     "    \"/api/v1/gpio/*\",        /* 单引脚 /api/v1/gpio/{pin}",
     "    \"/api/v1/gpio/*\",\n    \"/api/v1/events\",\n"
     "        /* 单引脚 /api/v1/gpio/{pin}",
     "test_events_is_not_in_generic_routes_table"),

    ("WS 端点用 HTTP_ANY 注册（握手不会被处理）",
     NET_C,
     '        .method   = HTTP_GET,\n        .handler  = events_ws_handler,',
     '        .method   = HTTP_ANY,\n        .handler  = events_ws_handler,',
     "test_ws_handler_registered_with_is_websocket"),

    ("WS 关闭帧处理被交给 httpd（自建逻辑失效）",
     NET_C,
     ".handle_ws_control_frames = false,",
     ".handle_ws_control_frames = true,",
     "test_ws_control_frames_not_disabled"),

    ("🔴 recv 前 frame.len 预填非 0 → 跳过帧头解析",
     NET_C,
     ".payload = buf, .len = 0,",
     ".payload = buf, .len = sizeof(buf),",
     "test_recv_frame_length_must_start_at_zero"),

    ("首帧不再回 status（客户端只握手收不到任何东西）",
     NET_C,
     '"{\\"type\\":\\"status\\",\\"echo\\":\\"%.*s\\"',
     '"{\\"type\\":\\"other\\",\\"echo\\":\\"%.*s\\"',
     "test_first_frame_returns_status_as_handshake_ack"),

    ("订阅分支混入 HTTP 响应（破坏 WS 流）",
     NET_C,
     "        httpd_ws_send_frame(req, &pf);",
     "        httpd_ws_send_frame(req, &pf);\n"
     "        httpd_resp_set_type(req, \"application/json\");\n"
     "        httpd_resp_send(req, pong, pn);",
     "test_ws_frames_never_mixed_with_http_responses"),

    ("订阅上限超过 max_open_sockets",
     NET_C,
     "#define EVENTS_SUB_MAX 4",
     "#define EVENTS_SUB_MAX 9",
     "test_subscriber_cap_within_socket_budget"),

    # ---- 普通 GET 判据（P4 遗留 bug：fd 复用 → GET /events 恒 0 字节）----
    ("普通 GET 改用「fd 在不在订阅表」判定（fd 复用即误判）",
     NET_C,
     "if (httpd_ws_get_fd_info(s_ev_srv, fd) != HTTPD_WS_CLIENT_WEBSOCKET) {",
     "if (ev_sub_find(fd) >= 0) {",
     "test_plain_get_detected_by_ws_session_state_not_subscription_table"),

    ("普通 GET 判据取反（WS 会话去收 HTTP 响应、HTTP 请求去收帧）",
     NET_C,
     "if (httpd_ws_get_fd_info(s_ev_srv, fd) != HTTPD_WS_CLIENT_WEBSOCKET) {",
     "if (httpd_ws_get_fd_info(s_ev_srv, fd) == HTTPD_WS_CLIENT_WEBSOCKET) {",
     "test_plain_get_detected_by_ws_session_state_not_subscription_table"),

    ("普通 GET 分支返回 ESP_FAIL（连接被直接关掉 = 原 bug 症状）",
     NET_C,
     "        return ESP_OK;   /* 正常响应：httpd 会自己关连接，语义正确 */",
     "        return ESP_FAIL;",
     "test_plain_get_path_returns_status_without_subscribing"),

    ("WS 收帧失败时摘掉订阅（又留一条僵尸条目）",
     NET_C,
     'ESP_LOGW(TAG, "events: ws recv failed on fd %d (%s), closing", fd, esp_err_to_name(ret));\n        ev_sub_del(fd);',
     'ESP_LOGW(TAG, "events: ws recv failed on fd %d (%s), closing", fd, esp_err_to_name(ret));',
     "test_ws_recv_failure_never_sends_http_response"),

    ("僵尸回收不递减计数（subscribers 永久虚高）",
     NET_C,
     "            s_ev_fd[i] = -1;\n            s_ev_sub_count--;\n"
     '            ESP_LOGI(TAG, "events: reap stale subscriber fd %d", fd);',
     "            s_ev_fd[i] = -1;\n"
     '            ESP_LOGI(TAG, "events: reap stale subscriber fd %d", fd);',
     "test_stale_subscriber_reaping_uses_public_ws_api"),

    ("broadcast 里调 reap（跨任务无锁遍历 httpd 会话表 → 竞态）",
     NET_C,
     "    int sent = 0;\n    for (int i = 0; i < EVENTS_SUB_MAX; i++) {\n"
     "        const int fd = s_ev_fd[i];\n        if (fd < 0) continue;",
     "    ev_sub_reap();\n    int sent = 0;\n"
     "    for (int i = 0; i < EVENTS_SUB_MAX; i++) {\n"
     "        const int fd = s_ev_fd[i];\n        if (fd < 0) continue;",
     "test_reaping_never_happens_from_non_httpd_task"),

    ("handler 里不 reap（订阅计数永远只增不减）",
     NET_C,
     "    ev_sub_reap();\n",
     "    /* reap removed */\n",
     "test_reaping_never_happens_from_non_httpd_task"),

    ("reap 挪到普通 GET 判定之后（subscribers 永远读旧值）",
     NET_C,
     "    ev_sub_reap();\n\n/* 🔴 该 URI 以 is_websocket=true 注册",
     "/* reap moved */\n\n/* 🔴 该 URI 以 is_websocket=true 注册",
     "test_reaping_happens_before_plain_get_branch"),

    ("max_uri_handlers 装不下所有 handler",
     NET_C,
     "cfg.max_uri_handlers = 32;",
     "cfg.max_uri_handlers = 8;",
     "test_max_uri_handlers_capacity"),

    ("routes.c 绕过接缝直接调驱动",
     ROUTES_C,
     "    int rc = ble_hid_send_key(req.key);",
     "    gpio_config(GPIO_NUM_19, 0);\n    int rc = ble_hid_send_key(req.key);",
     "test_routes_calls_seam_not_driver_directly"),

    ("枚举改回 gpio_mode_t（与 ESP-IDF 冲突）",
     ROUTES_C,
     "gpio_mode_e m = GPIO_MODE_IN;",
     "gpio_mode_t m = GPIO_MODE_IN;",
     None),   # platform.h 才有定义，这里改为直接改 platform.h
]


def _run():
    p = subprocess.run([PY, TEST], cwd=_ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    print("=" * 72)
    print("反向验证：破坏 → 期望测试变红 → 还原")
    print("=" * 72)

    base_rc, _ = _run()
    if base_rc != 0:
        print("[FATAL] 基线就不是绿的（exit=%d），先修好再谈变异测试" % base_rc)
        return 2
    print("基线：全绿 exit=0\n")

    backups = {}
    passed = failed = 0
    for desc, path, old, new, expect in MUTATIONS:
        if expect is None:
            continue
        src = io.open(path, encoding="utf-8").read()
        if old not in src:
            print("  [SKIP] %-56s 锚点未找到：%r" % (desc, old[:40]))
            failed += 1
            continue
        backups[path] = src
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(
                src.replace(old, new, 1))
            rc, out = _run()
            red = (rc != 0)
            named = expect in out
            if red and named:
                print("  [ OK ] %-56s → 变红且命中 %s" % (desc, expect))
                passed += 1
            else:
                print("  [BAD ] %-56s → rc=%d 命中=%s（守卫漏了！）"
                      % (desc, rc, named))
                failed += 1
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(src)

    # platform.h 的 gpio_mode_t 变异单独处理
    hdr = os.path.join(_ROOT, "firmware", "platform", "platform.h")
    src = io.open(hdr, encoding="utf-8").read()
    m = re.search(r"\} gpio_mode_e;", src)
    if m:
        io.open(hdr, "w", encoding="utf-8", newline="").write(
            src.replace("} gpio_mode_e;", "} gpio_mode_t;", 1))
        rc, out = _run()
        if rc != 0 and "test_gpio_mode_enum_name_avoids_idf_collision" in out:
            print("  [ OK ] %-56s → 变红" % "枚举改回 gpio_mode_t（与 IDF 冲突）")
            passed += 1
        else:
            print("  [BAD ] %-56s → rc=%d（守卫漏了！）" % ("gpio_mode_t 变异", rc))
            failed += 1
        io.open(hdr, "w", encoding="utf-8", newline="").write(src)
    else:
        print("  [SKIP] 未找到 '} gpio_mode_e;'")
        failed += 1

    rc, _ = _run()
    print("\n还原后复跑：exit=%d %s" % (rc, "（已恢复全绿）" if rc == 0 else "!! 仍未恢复"))
    print("结果：%d 条按预期变红，%d 条异常" % (passed, failed))
    return 0 if (failed == 0 and rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
